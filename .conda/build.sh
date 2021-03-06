#!/usr/bin/env bash

# Install anything that didn't get conda installed via pip.
# We need to turn pip index back on because Anaconda turns
# it off for some reason. Just pip install -r requirements.txt
# doesn't seem to work, tensorflow-gpu, jsonpickle, networkx,
# all get installed twice if we do this. pip doesn't see the
# conda install of the packages.

export PIP_NO_INDEX=False
export PIP_NO_DEPENDENCIES=False
export PIP_IGNORE_INSTALLED=False

pip install scipy==1.4.1 cattrs==1.0.0rc0 opencv-python-headless==4.2.0.34 "PySide2>=5.12.0,<=5.14.1" imgaug==0.3.0 qimage2ndarray==1.8 imgstore==0.2.9 jsmin seaborn scikit-video pykalman==0.9.5
#pip install tensorflow==2.1

pip install setuptools-scm

python setup.py install --single-version-externally-managed --record=record.txt
