import os
from pathlib import Path

from dotenv import load_dotenv
from hydra import compose, initialize_config_dir
from omegaconf import OmegaConf

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/.hydra_config")


def load_config(config_path=CONFIG_PATH, overrides=None) -> OmegaConf:
    load_dotenv()

    # TODO: I set the version base to 1.1 to silence the warning message, review how we want to handle versioning
    with initialize_config_dir(
        config_dir=config_path, job_name="config_loader", version_base="1.1"
    ):
        config = compose(config_name="config", overrides=overrides)
        config.paths.data_dir = Path(config.paths.data_dir).resolve()

        return config
