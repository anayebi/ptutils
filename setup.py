from setuptools import setup, find_packages
import os

if os.path.exists("requirements.txt"):
    with open("requirements.txt", "r") as fb:
        requirements = fb.readlines()
else:
    requirements = [
        "torch>=1.9",
        "torchvision>=0.10.0",
        "numpy>=1.20.3",
        "scikit-learn>=0.24.2",
        "scipy>=1.7.1",
        "pandas>=1.3.4",
        "black>=19.10b0",
        "pymongo>=3.11.1",
        "jsonpickle>=1.4.1",
        "shapely>=1.7.1",
        "regex",
        "google-cloud-storage",
    ]

print(find_packages())
setup(
    name="ptutils",
    version="0.1",
    packages=find_packages(),
    install_requires=requirements,
    python_requires=">=3.6",
    # metadata to display on PyPI
    description="Pytorch utilities for model training on GPU and TPU",
    # could also include long_description, download_url, etc.
)
