# Databricks notebook source
##################################################################################
# Model Training Notebook using Databricks Feature Store
#
# This notebook shows an example of a Model Training pipeline using Databricks Feature Store tables.
# It is configured and can be executed as a training-job.tf job defined under ``databricks-config``
#
# Parameters:
#
# * training_data_path (required)   - Path to the training data.
# * experiment_name (required)      - MLflow experiment name for the training runs. Will be created if it doesn't exist.
# * model_name (required)           - MLflow registered model name to use for the trained model. Will be created if it
# *                                   doesn't exist.
##################################################################################


# COMMAND ----------

# MAGIC %pip install -r ../requirements.txt

# COMMAND ----------

# DBTITLE 1, Notebook arguments
# List of input args needed to run this notebook as a job.
# Provide them via DB widgets or notebook arguments.

# Path to the Hive-registered Delta table containing the training data.
dbutils.widgets.text("training_data_path", "/user/hive/warehouse/invoices", label="Path to the training data")

# MLflow experiment name.
dbutils.widgets.text("experiment_name", "/Shared/my-mlops-project/my-mlops-project-experiment-test", label="MLflow experiment name")

# MLflow registered model name to use for the trained mode..
dbutils.widgets.text("model_name", "my-mlops-project-model-test", label="Model Name")

# Feature table stage.
dbutils.widgets.text("fs_stage", "_test", label="Input Feature Table Stage")

# COMMAND ----------

# DBTITLE 1,Define input and output variables
input_table_path = dbutils.widgets.get("training_data_path")
experiment_name = dbutils.widgets.get("experiment_name")
model_name = dbutils.widgets.get("model_name")
fs_stage = dbutils.widgets.get("fs_stage")

# COMMAND ----------

# DBTITLE 1, Set experiment
import mlflow

mlflow.set_experiment(experiment_name)

# COMMAND ----------

# DBTITLE 1, Load raw data
raw_data = spark.read.format("delta").load(input_table_path)
display(raw_data)

# COMMAND ----------

# DBTITLE 1, Helper functions

from pyspark.sql import *
from pyspark.sql.functions import current_timestamp, to_timestamp, lit
from pyspark.sql.types import IntegerType, TimestampType
import math
from datetime import timedelta, timezone
import mlflow.pyfunc


def preprocess(raw_df):
    # Round the taxi data timestamp to 15 and 30 minute intervals so we can join with the pickup and dropoff features
    # respectively.
    df = raw_df.toPandas()

    df.dropna(inplace=True)
    df['StockCode']= df['StockCode'].astype(str)
    products = df[["StockCode"]]
    products.drop_duplicates(inplace=True, subset='StockCode', keep="last")

    # create product-ID and product-description dictionary
    products_df=spark.createDataFrame(products) 

    products_df.createOrReplaceTempView("token_data")

    return products_df


def get_latest_model_version(model_name):
    latest_version = 1
    mlflow_client = MlflowClient()
    for mv in mlflow_client.search_model_versions(f"name='{model_name}'"):
        version_int = int(mv.version)
        if version_int > latest_version:
            latest_version = version_int
    return latest_version


# COMMAND ----------

# DBTITLE 1, Read invoices data for training
product_descriptions_df = preprocess(raw_data)
display(product_descriptions_df)

# COMMAND ----------

# DBTITLE 1, Create FeatureLookups
from databricks.feature_store import FeatureLookup
import mlflow

pickup_features_table = "feature_store_product.descriptions"+fs_stage

product_description_feature_lookups = [
    FeatureLookup(
        table_name = pickup_features_table,
        feature_names = ["description_preprocessed"],
        lookup_key = ["StockCode"],
#        timestamp_lookup_key = ["rounded_pickup_datetime"]
    ),
]

# COMMAND ----------

# DBTITLE 1, Create Training Dataset
from databricks import feature_store

# End any existing runs (in the case this notebook is being run for a second time)
mlflow.end_run()

# Start an mlflow run, which is needed for the feature store to log the model
mlflow.start_run()

# Since the rounded timestamp columns would likely cause the model to overfit the data 
# unless additional feature engineering was performed, exclude them to avoid training on them.
#exclude_columns = ["rounded_pickup_datetime", "rounded_dropoff_datetime"]

fs = feature_store.FeatureStoreClient()

# Create the training set that includes the raw input data merged with corresponding features from both feature tables
training_set = fs.create_training_set(
    product_descriptions_df,
    feature_lookups = product_description_feature_lookups,
    #exclude_columns = exclude_columns,
    label=None 
)

# Load the TrainingSet into a dataframe which can be passed into sklearn for training a model
training_df = training_set.load_df()

# COMMAND ----------

# Display the training dataframe, and note that it contains both the raw input data and the features from the Feature Store, like `dropoff_is_weekend`
display(training_df)

# COMMAND ----------

# MAGIC %md
# MAGIC Train a LightGBM model on the data returned by `TrainingSet.to_df`, then log the model with `FeatureStoreClient.log_model`. The model will be packaged with feature metadata.

# COMMAND ----------

# DBTITLE 1, Train model
from sklearn.model_selection import train_test_split
from mlflow.tracking import MlflowClient
from sys import version_info
from tqdm import tqdm

from gensim.models import Word2Vec 

#import umap
#import matplotlib.pyplot as plt
#%matplotlib inline
import warnings;
warnings.filterwarnings('ignore')

import mlflow
import mlflow.pyfunc

import sys
sys.path.append("../steps")

from steps.models.word2vec_wrapper import GensimModelWrapper

features_and_label = training_df.columns

# Collect data into a Pandas array for training
data = training_df.toPandas()["description_preprocessed"].apply(lambda elem: list(elem)).to_list()  

#train, test = train_test_split(data, random_state=123)
#X_train = train.drop(["fare_amount"], axis=1)
#X_test = test.drop(["fare_amount"], axis=1)
#y_train = train.fare_amount
#y_test = test.fare_amount

#mlflow.lightgbm.autolog()
#train_lgb_dataset = lgb.Dataset(X_train, label=y_train.values)
#test_lgb_dataset = lgb.Dataset(X_test, label=y_test.values)

#param = {"num_leaves": 32, "objective": "regression", "metric": "rmse"}

# Hyperparameters
window=10
sg=1
hs=0
negative=10
alpha=0.03
min_alpha=0.0007
seed=14
epochs=10

mlflow.log_param('window', window)
mlflow.log_param('sg', sg)
mlflow.log_param('hs', hs)
mlflow.log_param('negative', negative)
mlflow.log_param('alpha', alpha)
mlflow.log_param('min_alpha', min_alpha)
mlflow.log_param('seed', seed)
mlflow.log_param('epochs', epochs)


# train word2vec model
model = Word2Vec(
    window = window, 
    sg = sg, 
    hs = hs,
    negative = negative, # for negative sampling
    alpha=alpha, 
    min_alpha=min_alpha,
    seed = seed
)

#num_rounds = 100
# Train a lightGBM model
#model = lgb.train(
#    param, train_lgb_dataset, num_rounds
#)

model.build_vocab(data, progress_per=200)

model.train(
    data, 
    total_examples = model.corpus_count, 
    epochs=epochs, 
    report_delay=1
)

print(model.wv.vocab)
#model.save("word2vec.model")
#artifacts = {"gensim_model": word2vec.model}
#mlflow_pyfunc_model_path = model_name

wrappedModel = GensimModelWrapper(model)

# COMMAND ----------

import pandas as pd
wrappedModel.predict(None, training_df.toPandas())

# COMMAND ----------

# DBTITLE 1, Log model and return output.
# Log the trained model with MLflow and package it with feature lookup information.

fs.log_model(
    wrappedModel,
    artifact_path="model_packaged",
    flavor=mlflow.pyfunc,
    training_set=training_set,
    registered_model_name=model_name
)

# Build out the MLflow model registry URL for this model version.
workspace_url = spark.conf.get("spark.databricks.workspaceUrl")
model_version = get_latest_model_version(model_name)
model_registry_url = "https://{workspace_url}/#mlflow/models/{model_name}/versions/{model_version}"\
    .format(workspace_url=workspace_url, model_name=model_name, model_version=model_version)

# The returned model URI is needed by the model deployment notebook.
model_uri = f"models:/{model_name}/{model_version}"
dbutils.jobs.taskValues.set("model_uri", model_uri)
dbutils.jobs.taskValues.set("model_name", model_name)
dbutils.jobs.taskValues.set("model_version", model_version)
dbutils.notebook.exit(model_uri)
