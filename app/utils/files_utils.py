from pathlib import Path
import importlib.resources

def read_file_from_init(filename: str, package: str) -> str:
    with importlib.resources.files(package).joinpath(filename).open("r", encoding="utf-8") as f:
        return f.read()
    