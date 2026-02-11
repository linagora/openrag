import os
from pathlib import Path

from dotenv import load_dotenv
from hydra import compose, initialize_config_dir
from hydra.core.global_hydra import GlobalHydra
from omegaconf import OmegaConf

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/app/.hydra_config")).resolve()


def load_config(config_path=CONFIG_PATH, overrides=None) -> OmegaConf:
    load_dotenv()

    # Clear existing Hydra instance to prevent "already initialized" errors
    if GlobalHydra.instance().is_initialized():
        GlobalHydra.instance().clear()

    # version_base=None uses current Hydra version defaults (forward compatible)
    with initialize_config_dir(config_dir=str(config_path), job_name="config_loader", version_base=None):
        config = compose(config_name="config", overrides=overrides)

        config.paths.data_dir = Path(config.paths.data_dir).resolve()
        config.paths.log_dir = Path(config.paths.log_dir).resolve()
        config.paths.prompts_dir = Path(config.paths.prompts_dir).resolve()

        return config
