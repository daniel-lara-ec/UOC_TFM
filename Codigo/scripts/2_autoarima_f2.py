## Librerías
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import mlflow
from pmdarima import auto_arima
from statsmodels.tsa.stattools import adfuller


## Configuracion centralizada
DATA_CONFIG = {
    "train_path": "data/conjuntos/datos_entrenamiento.csv",
    "date_col": "FechaMedicion",
    "target_col": "Potencia",
    "phase_col": "Fase",
    "phase_value": 2,
}

EXPERIMENT_CONFIG = {
    "tracking_uri": "",
    "experiment_name": "TFM_Modelado_ARIMA_F2",
    "test_size": 6,
    "future_steps": 6,
    "seasonal_periods": [6, 24, 144, 1008],
    "predictions_output_path": "../predicciones_autoarima_f2.csv",
}

AUTO_ARIMA_CONFIG = {
    "seasonal": True,
    "stepwise": True,
    "suppress_warnings": True,
    "error_action": "ignore",
    "trace": True,
    "start_p": 0,
    "start_q": 0,
    "max_p": 3,
    "max_q": 3,
    "max_P": 2,
    "max_Q": 2,
    "maxiter": 50,
}

ADF_CONFIG = {
    "autolag": "AIC",
}


def _to_mlflow_params(prefix, data):
    return {f"{prefix}.{k}": str(v) for k, v in data.items()}


def _save_and_log_csv(df, path, artifact_subdir):
    df.to_csv(path, index=True)
    mlflow.log_artifact(str(path), artifact_path=artifact_subdir)


mlflow.set_tracking_uri(EXPERIMENT_CONFIG["tracking_uri"])
mlflow.set_experiment(EXPERIMENT_CONFIG["experiment_name"])

## Carga de datos
DATE_COL = DATA_CONFIG["date_col"]
TARGET_COL = DATA_CONFIG["target_col"]

df_train = pd.read_csv(DATA_CONFIG["train_path"], parse_dates=[DATE_COL])
print(df_train.info())
df_train.head()

lista_s = EXPERIMENT_CONFIG["seasonal_periods"]

# Datos F2
ts_2 = (
    df_train[df_train[DATA_CONFIG["phase_col"]] == DATA_CONFIG["phase_value"]][
        [DATE_COL, TARGET_COL]
    ]
    .dropna(subset=[DATE_COL])
    .sort_values(DATE_COL)
    .set_index(DATE_COL)[TARGET_COL]
)

ts_2 = ts_2.interpolate(method="time").ffill().bfill()

print("Observaciones finales:", len(ts_2))
print(ts_2.tail(5).to_frame(name=TARGET_COL))

test_size = EXPERIMENT_CONFIG["test_size"]
future_steps = EXPERIMENT_CONFIG["future_steps"]

train = ts_2.iloc[:-test_size]
test = ts_2.iloc[-test_size:]

with TemporaryDirectory(prefix="mlflow_autoarima_") as temp_artifacts_dir:
    artifacts_dir = Path(temp_artifacts_dir)
    metrics_rows = []

    with mlflow.start_run(run_name="autoarima_fase2_parent"):
        mlflow.log_params(_to_mlflow_params("data", DATA_CONFIG))
        mlflow.log_params(_to_mlflow_params("experiment", EXPERIMENT_CONFIG))
        mlflow.log_params(_to_mlflow_params("auto_arima", AUTO_ARIMA_CONFIG))
        mlflow.log_params(_to_mlflow_params("adf", ADF_CONFIG))
        mlflow.set_tag("model_family", "ARIMA")
        mlflow.set_tag("phase", str(DATA_CONFIG["phase_value"]))

        # Datasets base del experimento
        mlflow.log_artifact(DATA_CONFIG["train_path"], artifact_path="datasets")
        ts_2_df = ts_2.to_frame(name=TARGET_COL)
        train_df = train.to_frame(name=TARGET_COL)
        test_df = test.to_frame(name=TARGET_COL)

        _save_and_log_csv(
            ts_2_df,
            artifacts_dir / "serie_fase2_completa.csv",
            "datasets",
        )
        _save_and_log_csv(train_df, artifacts_dir / "serie_train.csv", "datasets")
        _save_and_log_csv(test_df, artifacts_dir / "serie_test.csv", "datasets")

        # Grafico base de la serie
        fig, ax = plt.subplots(figsize=(14, 4))
        ax.plot(
            np.asarray(ts_2.index),
            np.asarray(ts_2.values, dtype=float),
            color="tab:blue",
            linewidth=1,
        )
        ax.set_title("Serie temporal original")
        ax.set_xlabel("Fecha")
        ax.set_ylabel(TARGET_COL)
        ax.grid(alpha=0.25)
        fig.tight_layout()
        plot_path = artifacts_dir / "serie_original.png"
        fig.savefig(plot_path, dpi=140)
        plt.close(fig)
        mlflow.log_artifact(str(plot_path), artifact_path="plots")

        for s in lista_s:
            with mlflow.start_run(run_name=f"autoarima_s_{s}", nested=True):
                print(f"Probando estacionariedad con ADF para s={s}...")
                adf_result = adfuller(train, autolag=ADF_CONFIG["autolag"], maxlag=s)
                print(f"ADF Statistic: {adf_result[0]:.4f}")
                print(f"p-value: {adf_result[1]:.4f}")
                print("-" * 30)

                mlflow.log_param("seasonal_period", s)
                mlflow.log_metric("adf_statistic", float(adf_result[0]))
                mlflow.log_metric("adf_pvalue", float(adf_result[1]))

                # Ejecutamos AutoARIMA capturando el trace de búsqueda
                trace_output = StringIO()
                with redirect_stdout(trace_output):
                    model = auto_arima(
                        train,
                        m=s,
                        **AUTO_ARIMA_CONFIG,
                    )
                trace_text = trace_output.getvalue()

                print(model.summary())
                mlflow.log_text(model.summary().as_text(), f"model/summary_s_{s}.txt")

                # Loguea el trace de búsqueda stepwise para análisis de modelos probados
                if trace_text:
                    mlflow.log_text(trace_text, f"model/search_trace_s_{s}.txt")

                mlflow.log_param("order", str(model.order))
                mlflow.log_param("seasonal_order", str(model.seasonal_order))
                mlflow.log_param("aic", float(model.aic()))

                ### Predecimos
                test_pred_raw = model.predict(n_periods=test_size)
                test_pred = pd.Series(
                    np.asarray(test_pred_raw), index=test.index, name="pred_test"
                )

                mae = float((test - test_pred).abs().mean())
                rmse = float(np.sqrt(((test - test_pred) ** 2).mean()))
                mape = float(
                    (((test - test_pred).abs() / (test.abs() + 1e-9)).mean()) * 100
                )

                print(f"MAE: {mae:.4f}")
                print(f"RMSE: {rmse:.4f}")
                print(f"MAPE: {mape:.4f}%")
                mlflow.log_metric("mae", mae)
                mlflow.log_metric("rmse", rmse)
                mlflow.log_metric("mape", mape)

                future_pred_raw = model.predict(n_periods=future_steps)

                # Usa la frecuencia real del indice para que el horizonte futuro no desalinee con los valores predichos.
                ts_index = pd.DatetimeIndex(ts_2.index)
                freq_for_future = pd.infer_freq(ts_index)
                if freq_for_future is None:
                    deltas = ts_index.to_series().diff().dropna()
                    if len(deltas) == 0:
                        raise ValueError(
                            "No se pudo inferir frecuencia de la serie temporal."
                        )
                    freq_for_future = deltas.mode().iloc[0]

                future_index = pd.date_range(
                    start=ts_index.max(), periods=future_steps + 1, freq=freq_for_future
                )[1:]
                future_forecast = pd.Series(
                    np.asarray(future_pred_raw), index=future_index, name="pred_futuro"
                )

                predicciones = pd.concat(
                    [test.rename("real_test"), test_pred, future_forecast], axis=1
                )
                print(predicciones.head(10))
                print(predicciones.tail(10))

                predicciones_path = artifacts_dir / f"predicciones_s_{s}.csv"
                predicciones.to_csv(predicciones_path, index=True)
                mlflow.log_artifact(
                    str(predicciones_path), artifact_path="predicciones"
                )

                # Conserva salida local consolidada para inspeccion rapida fuera de MLflow.
                predicciones.to_csv(
                    EXPERIMENT_CONFIG["predictions_output_path"], index=True
                )
                print(
                    f"Archivo guardado en {EXPERIMENT_CONFIG['predictions_output_path']}"
                )

                ## Graficamos la predicción
                fig, ax = plt.subplots(figsize=(14, 5))

                # Muestra un tramo reciente fijo para comparar con Prophet.
                window = min(len(ts_2), test_size)
                ts_view = ts_2.iloc[-window:]

                ax.plot(
                    np.asarray(ts_view.index),
                    np.asarray(ts_view.values, dtype=float),
                    label="Serie real (reciente)",
                    color="tab:blue",
                    linewidth=1.5,
                )
                ax.plot(
                    np.asarray(test.index),
                    np.asarray(test.values, dtype=float),
                    label="Real test",
                    color="tab:green",
                    linewidth=2,
                )
                ax.plot(
                    np.asarray(test_pred.index),
                    np.asarray(test_pred.values, dtype=float),
                    label="Prediccion test",
                    color="tab:orange",
                    linestyle="--",
                    linewidth=2,
                )
                ax.plot(
                    np.asarray(future_forecast.index),
                    np.asarray(future_forecast.values, dtype=float),
                    label="Proyeccion futura",
                    color="tab:red",
                    linestyle=":",
                    linewidth=2.5,
                )

                ax.set_title("Serie real y serie proyectada")
                ax.set_xlabel("Fecha")
                ax.set_ylabel(TARGET_COL)
                ax.legend()
                ax.grid(alpha=0.25)
                fig.tight_layout()
                forecast_plot_path = artifacts_dir / f"forecast_s_{s}.png"
                fig.savefig(forecast_plot_path, dpi=140)
                plt.close(fig)
                mlflow.log_artifact(str(forecast_plot_path), artifact_path="plots")

                metrics_rows.append(
                    {
                        "s": s,
                        "adf_statistic": float(adf_result[0]),
                        "adf_pvalue": float(adf_result[1]),
                        "mae": mae,
                        "rmse": rmse,
                        "mape": mape,
                        "order": str(model.order),
                        "seasonal_order": str(model.seasonal_order),
                        "aic": float(model.aic()),
                    }
                )

        summary_metrics_df = pd.DataFrame(metrics_rows)
        summary_metrics_path = artifacts_dir / "metricas_resumen_por_s.csv"
        summary_metrics_df.to_csv(summary_metrics_path, index=False)
        mlflow.log_artifact(str(summary_metrics_path), artifact_path="resumen")

        # Identifica y registra el mejor modelo por RMSE
        best_rmse_idx = summary_metrics_df["rmse"].idxmin()
        best_rmse_s = summary_metrics_df.loc[best_rmse_idx, "s"]
        best_rmse_value = summary_metrics_df.loc[best_rmse_idx, "rmse"]

        # Identifica y registra el mejor modelo por MAPE
        best_mape_idx = summary_metrics_df["mape"].idxmin()
        best_mape_s = summary_metrics_df.loc[best_mape_idx, "s"]
        best_mape_value = summary_metrics_df.loc[best_mape_idx, "mape"]

        # Registra mejores modelos como resumen
        best_models_summary = {
            "best_by_rmse": {
                "s": int(best_rmse_s),
                "rmse": float(best_rmse_value),
                "mae": float(summary_metrics_df.loc[best_rmse_idx, "mae"]),
                "mape": float(summary_metrics_df.loc[best_rmse_idx, "mape"]),
                "order": summary_metrics_df.loc[best_rmse_idx, "order"],
                "seasonal_order": summary_metrics_df.loc[
                    best_rmse_idx, "seasonal_order"
                ],
            },
            "best_by_mape": {
                "s": int(best_mape_s),
                "mape": float(best_mape_value),
                "rmse": float(summary_metrics_df.loc[best_mape_idx, "rmse"]),
                "mae": float(summary_metrics_df.loc[best_mape_idx, "mae"]),
                "order": summary_metrics_df.loc[best_mape_idx, "order"],
                "seasonal_order": summary_metrics_df.loc[
                    best_mape_idx, "seasonal_order"
                ],
            },
        }

        best_models_json_path = artifacts_dir / "mejores_modelos.json"
        import json

        with open(best_models_json_path, "w") as f:
            json.dump(best_models_summary, f, indent=2)
        mlflow.log_artifact(str(best_models_json_path), artifact_path="resumen")

        # Registra métricas globales al run padre
        mlflow.log_metric("best_rmse_across_models", best_rmse_value)
        mlflow.log_metric("best_mape_across_models", best_mape_value)
        mlflow.log_metric("mean_rmse", summary_metrics_df["rmse"].mean())
        mlflow.log_metric("mean_mape", summary_metrics_df["mape"].mean())
        mlflow.log_metric("std_rmse", summary_metrics_df["rmse"].std())
        mlflow.log_metric("std_mape", summary_metrics_df["mape"].std())

        # Crea visualizacion comparativa de métricas
        fig_comp, axes = plt.subplots(2, 2, figsize=(16, 10))

        axes[0, 0].plot(
            summary_metrics_df["s"], summary_metrics_df["rmse"], marker="o", linewidth=2
        )
        axes[0, 0].axhline(
            best_rmse_value,
            color="red",
            linestyle="--",
            label=f"Best RMSE: s={best_rmse_s}",
        )
        axes[0, 0].set_xlabel("Seasonal Period (s)")
        axes[0, 0].set_ylabel("RMSE")
        axes[0, 0].set_title("RMSE por Período Estacional")
        axes[0, 0].legend()
        axes[0, 0].grid(alpha=0.25)

        axes[0, 1].plot(
            summary_metrics_df["s"],
            summary_metrics_df["mape"],
            marker="s",
            linewidth=2,
            color="orange",
        )
        axes[0, 1].axhline(
            best_mape_value,
            color="red",
            linestyle="--",
            label=f"Best MAPE: s={best_mape_s}",
        )
        axes[0, 1].set_xlabel("Seasonal Period (s)")
        axes[0, 1].set_ylabel("MAPE (%)")
        axes[0, 1].set_title("MAPE por Período Estacional")
        axes[0, 1].legend()
        axes[0, 1].grid(alpha=0.25)

        axes[1, 0].plot(
            summary_metrics_df["s"],
            summary_metrics_df["mae"],
            marker="^",
            linewidth=2,
            color="green",
        )
        axes[1, 0].set_xlabel("Seasonal Period (s)")
        axes[1, 0].set_ylabel("MAE")
        axes[1, 0].set_title("MAE por Período Estacional")
        axes[1, 0].grid(alpha=0.25)

        axes[1, 1].plot(
            summary_metrics_df["s"],
            summary_metrics_df["adf_pvalue"],
            marker="d",
            linewidth=2,
            color="purple",
        )
        axes[1, 1].axhline(0.05, color="red", linestyle="--", label="Significance=0.05")
        axes[1, 1].set_xlabel("Seasonal Period (s)")
        axes[1, 1].set_ylabel("ADF p-value")
        axes[1, 1].set_title("ADF p-value por Período Estacional")
        axes[1, 1].legend()
        axes[1, 1].grid(alpha=0.25)

        fig_comp.tight_layout()
        comparison_plot_path = artifacts_dir / "comparativa_metricas.png"
        fig_comp.savefig(comparison_plot_path, dpi=140)
        plt.close(fig_comp)
        mlflow.log_artifact(str(comparison_plot_path), artifact_path="comparativas")

        # Metadata del experimento
        metadata = {
            "experiment_name": EXPERIMENT_CONFIG["experiment_name"],
            "data_source": DATA_CONFIG["train_path"],
            "phase_filtered": DATA_CONFIG["phase_value"],
            "total_observations": len(ts_2),
            "train_size": len(train),
            "test_size": len(test),
            "future_forecast_steps": future_steps,
            "seasonal_periods_tested": lista_s,
            "num_models_trained": len(lista_s),
            "auto_arima_max_p": AUTO_ARIMA_CONFIG["max_p"],
            "auto_arima_max_q": AUTO_ARIMA_CONFIG["max_q"],
            "auto_arima_max_P": AUTO_ARIMA_CONFIG["max_P"],
            "auto_arima_max_Q": AUTO_ARIMA_CONFIG["max_Q"],
        }

        metadata_path = artifacts_dir / "metadata_experimento.json"
        with open(metadata_path, "w") as f:
            json.dump(metadata, f, indent=2)
        mlflow.log_artifact(str(metadata_path), artifact_path="metadata")

        # Estadísticas descriptivas de la serie
        series_stats = {
            "mean": float(ts_2.mean()),
            "std": float(ts_2.std()),
            "min": float(ts_2.min()),
            "max": float(ts_2.max()),
            "median": float(ts_2.median()),
            "q25": float(ts_2.quantile(0.25)),
            "q75": float(ts_2.quantile(0.75)),
            "skew": float(ts_2.skew()),
            "kurtosis": float(ts_2.kurtosis()),
        }

        series_stats_path = artifacts_dir / "estadisticas_serie.json"
        with open(series_stats_path, "w") as f:
            json.dump(series_stats, f, indent=2)
        mlflow.log_artifact(str(series_stats_path), artifact_path="metadata")

        # Registra parámetros de mejores modelos como parámetros del run padre
        mlflow.log_param("best_model_s_by_rmse", int(best_rmse_s))
        mlflow.log_param("best_model_s_by_mape", int(best_mape_s))

        # Consolidar CSV final con las predicciones del mejor modelo (RMSE)
        best_predictions_df = summary_metrics_df[summary_metrics_df["s"] == best_rmse_s]
        if len(best_predictions_df) > 0:
            best_predictions_path = (
                artifacts_dir / f"predicciones_MEJOR_MODELO_s_{best_rmse_s}.csv"
            )
            # Vuelve a leer las predicciones guardadas para este s
            best_pred_input_path = artifacts_dir / f"predicciones_s_{best_rmse_s}.csv"
            if best_pred_input_path.exists():
                best_predictions_final = pd.read_csv(
                    best_pred_input_path, index_col=0, parse_dates=True
                )
                best_predictions_final.to_csv(best_predictions_path)
                mlflow.log_artifact(
                    str(best_predictions_path), artifact_path="best_model"
                )
            else:
                # Si no existe, recrearla no es viable sin reguardar, así que loguea la intención
                mlflow.log_text(
                    f"Best model by RMSE: s={best_rmse_s} with RMSE={best_rmse_value:.4f}",
                    "best_model/seleccion.txt",
                )

        print("\n" + "=" * 60)
        print("RESUMEN FINAL DEL EXPERIMENTO - FASE 2")
        print("=" * 60)
        print(f"\nMejor modelo por RMSE: s={best_rmse_s} (RMSE={best_rmse_value:.4f})")
        print(f"Mejor modelo por MAPE: s={best_mape_s} (MAPE={best_mape_value:.4f}%)")
        print(f"\nTotal de observaciones: {len(ts_2)}")
        print(f"Train: {len(train)}, Test: {len(test)}")
        print(f"Modelos entrenados: {len(lista_s)}")
        print("\n" + "=" * 60)

        # Resumen de recursos: logs de trazas disponibles en MLflow bajo model/search_trace_s_{s}.txt
        print("\n📊 Análisis de modelos probados:")
        print("   Todos los traces de búsqueda stepwise están disponibles en MLflow:")
        for s in lista_s:
            print(f"   • model/search_trace_s_{s}.txt → Run anidado autoarima_s_{s}")
