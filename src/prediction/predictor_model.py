import os
import warnings
import joblib
import numpy as np
import pandas as pd
from typing import Optional, Union, List
from darts.models.forecasting.tbats_model import TBATS
from darts import TimeSeries
from schema.data_schema import ForecastingSchema
from sklearn.exceptions import NotFittedError
from multiprocessing import cpu_count, Pool

warnings.filterwarnings("ignore")
PREDICTOR_FILE_NAME = "predictor.joblib"

# Determine the number of CPUs available
CPUS_TO_USE = max(1, cpu_count() - 1) # spare one CPU for other tasks
NUM_CPUS_PER_BATCH = 1    # Number of CPUs each batch can use


class Forecaster:
    """A wrapper class for the TBATS Forecaster.

    This class provides a consistent interface that can be used with other
    Forecaster models.
    """

    model_name = "TBATS Forecaster"

    def __init__(
        self,
        data_schema: ForecastingSchema,
        history_forecast_ratio: int = None,
        use_box_cox: Optional[bool] = None,
        box_cox_bounds: tuple = (0, 1),
        use_trend: Optional[bool] = None,
        use_damped_trend: Optional[bool] = None,
        seasonal_periods: Union[str, List, None] = "freq",
        use_arma_errors: Optional[bool] = True,
        random_state: int = 0,
    ):
        """Construct a new TBATS Forecaster

        Args:

            data_schema (ForecastingSchema):
                Schema of training data.

            history_forecast_ratio (int):
                Sets the history length depending on the forecast horizon.
                For example, if the forecast horizon is 20 and the history_forecast_ratio is 10,
                history length will be 20*10 = 200 samples.

            use_box_cox (Optional[bool]): If Box-Cox transformation of original series should be applied.
                When None both cases shall be considered and better is selected by AIC.

            box_cox_bounds (Tuple): Minimal and maximal Box-Cox parameter values.

            use_trend (Optional[bool]): Indicates whether to include a trend or not.
                When None, both cases shall be considered and the better one is selected by AIC.

            use_damped_trend (Optional[bool]): Indicates whether to include a damping parameter in the trend or not.
                Applies only when trend is used. When None, both cases shall be considered and the better one is selected by AIC.

            seasonal_periods (Union[str, List, None]): Length of each of the periods (amount of observations in each period).
                TTBATS accepts int and float values here. TBATS accepts only int values.
                When None or empty array, non-seasonal model shall be fitted.
                If set to "freq", a single “naive” seasonality based on the series frequency will be used (e.g. [12] for monthly series).
                In this latter case, the seasonality will be recomputed every time the model is fit.

            use_arma_errors (Optional[bool]): When True TBATS will try to improve the model by modelling residuals with ARMA.
                Best model will be selected by AIC. If False, ARMA residuals modeling will not be considered.

            random_state (int): Sets the underlying random seed at model initialization time.
        """
        self.data_schema = data_schema
        self.use_box_cox = use_box_cox
        self.box_cox_bounds = box_cox_bounds
        self.use_trend = use_trend
        self.use_damped_trend = use_damped_trend
        self.seasonal_periods = seasonal_periods
        self.use_arma_errors = use_arma_errors
        self.random_state = random_state
        self._is_trained = False
        self.models = {}
        self.history_length = None

        if history_forecast_ratio:
            self.history_length = (
                self.data_schema.forecast_length * history_forecast_ratio
            )

    def fit(
        self,
        history: pd.DataFrame,
        data_schema: ForecastingSchema,
    ) -> None:
        np.random.seed(self.random_state)
        groups_by_ids = history.groupby(data_schema.id_col)
        all_ids = list(groups_by_ids.groups.keys())
        all_series = [
            groups_by_ids.get_group(id_).drop(columns=data_schema.id_col)
            for id_ in all_ids
        ]

        # Prepare batches of series to be processed in parallel
        num_parallel_batches = CPUS_TO_USE // NUM_CPUS_PER_BATCH
        if len(all_ids) <= num_parallel_batches:
            series_per_batch = 1
        else:
            series_per_batch = 1 + (len(all_ids) // num_parallel_batches)
        series_batches = [
            all_series[i:i + series_per_batch]
            for i in range(0, len(all_series), series_per_batch)
        ]
        id_batches = [
            all_ids[i:i + series_per_batch]
            for i in range(0, len(all_ids), series_per_batch)
        ]

        # Use multiprocessing to fit models in parallel
        with Pool(processes=len(series_batches)) as pool:
            results = pool.starmap(
                self.fit_batch_of_series,
                zip(series_batches, id_batches, [data_schema] * len(series_batches))
            )

        # Flatten results and update the models dictionary
        self.models = {id: model for batch in results for id, model in batch.items()}       
        
        self.all_ids = all_ids
        self._is_trained = True
        self.data_schema = data_schema

    def fit_batch_of_series(self, series_batch, ids_batch, data_schema):
        models = {}
        for series, id in zip(series_batch, ids_batch):
            if self.history_length:
                series = series[-self.history_length:]
            model = self._fit_on_series(history=series, data_schema=data_schema)
            models[id] = model
        return models

    def _fit_on_series(self, history: pd.DataFrame, data_schema: ForecastingSchema):
        """Fit TBATS model to given individual series of data"""
        model = TBATS(
            use_box_cox=self.use_box_cox,
            box_cox_bounds=self.box_cox_bounds,
            use_trend=self.use_trend,
            use_damped_trend=self.use_damped_trend,
            seasonal_periods=self.seasonal_periods,
            use_arma_errors=self.use_arma_errors,
            show_warnings=False,
            n_jobs=NUM_CPUS_PER_BATCH,
            multiprocessing_start_method="spawn",
            random_state=self.random_state,
        )

        series = TimeSeries.from_dataframe(
            history, data_schema.time_col, data_schema.target
        )
        model.fit(series)

        return model

    def predict(self, test_data: pd.DataFrame, prediction_col_name: str) -> np.ndarray:
        """Make the forecast of given length.

        Args:
            test_data (pd.DataFrame): Given test input for forecasting.
            prediction_col_name (str): Name to give to prediction column.
        Returns:
            numpy.ndarray: The predicted class labels.
        """
        if not self._is_trained:
            raise NotFittedError("Model is not fitted yet.")

        groups_by_ids = test_data.groupby(self.data_schema.id_col)
        all_series = [
            groups_by_ids.get_group(id_).drop(columns=self.data_schema.id_col)
            for id_ in self.all_ids
        ]
        # forecast one series at a time
        all_forecasts = []
        for id_, series_df in zip(self.all_ids, all_series):
            forecast = self._predict_on_series(key_and_future_df=(id_, series_df))
            forecast.insert(0, self.data_schema.id_col, id_)
            all_forecasts.append(forecast)

        # concatenate all series' forecasts into a single dataframe
        all_forecasts = pd.concat(all_forecasts, axis=0, ignore_index=True)

        all_forecasts.rename(
            columns={self.data_schema.target: prediction_col_name}, inplace=True
        )
        return all_forecasts

    def _predict_on_series(self, key_and_future_df):
        """Make forecast on given individual series of data"""
        key, future_df = key_and_future_df

        if self.models.get(key) is not None:
            forecast = self.models[key].predict(len(future_df))
            forecast_df = forecast.pd_dataframe()
            forecast = forecast_df[self.data_schema.target]
            future_df[self.data_schema.target] = forecast.values

        else:
            # no model found - key wasnt found in history, so cant forecast for it.
            future_df = None

        return future_df

    def save(self, model_dir_path: str) -> None:
        """Save the Forecaster to disk.

        Args:
            model_dir_path (str): Dir path to which to save the model.
        """
        if not self._is_trained:
            raise NotFittedError("Model is not fitted yet.")
        joblib.dump(self, os.path.join(model_dir_path, PREDICTOR_FILE_NAME))

    @classmethod
    def load(cls, model_dir_path: str) -> "Forecaster":
        """Load the Forecaster from disk.

        Args:
            model_dir_path (str): Dir path to the saved model.
        Returns:
            Forecaster: A new instance of the loaded Forecaster.
        """
        model = joblib.load(os.path.join(model_dir_path, PREDICTOR_FILE_NAME))
        return model

    def __str__(self):
        # sort params alphabetically for unit test to run successfully
        return f"Model name: {self.model_name}"


def train_predictor_model(
    history: pd.DataFrame,
    data_schema: ForecastingSchema,
    hyperparameters: dict,
) -> Forecaster:
    """
    Instantiate and train the predictor model.

    Args:
        history (pd.DataFrame): The training data inputs.
        data_schema (ForecastingSchema): Schema of the training data.
        hyperparameters (dict): Hyperparameters for the Forecaster.

    Returns:
        'Forecaster': The Forecaster model
    """

    model = Forecaster(
        data_schema=data_schema,
        **hyperparameters,
    )
    model.fit(history=history, data_schema=data_schema)
    return model


def predict_with_model(
    model: Forecaster, test_data: pd.DataFrame, prediction_col_name: str
) -> pd.DataFrame:
    """
    Make forecast.

    Args:
        model (Forecaster): The Forecaster model.
        test_data (pd.DataFrame): The test input data for forecasting.
        prediction_col_name (int): Name to give to prediction column.

    Returns:
        pd.DataFrame: The forecast.
    """
    return model.predict(test_data, prediction_col_name)


def save_predictor_model(model: Forecaster, predictor_dir_path: str) -> None:
    """
    Save the Forecaster model to disk.

    Args:
        model (Forecaster): The Forecaster model to save.
        predictor_dir_path (str): Dir path to which to save the model.
    """
    if not os.path.exists(predictor_dir_path):
        os.makedirs(predictor_dir_path)
    model.save(predictor_dir_path)


def load_predictor_model(predictor_dir_path: str) -> Forecaster:
    """
    Load the Forecaster model from disk.

    Args:
        predictor_dir_path (str): Dir path where model is saved.

    Returns:
        Forecaster: A new instance of the loaded Forecaster model.
    """
    return Forecaster.load(predictor_dir_path)
