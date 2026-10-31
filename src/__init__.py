"""transcript-pipeline package."""

__all__ = ["load_settings"]


def __getattr__(name: str):
    if name == "load_settings":
        from src.config_loader import load_settings
        return load_settings
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
