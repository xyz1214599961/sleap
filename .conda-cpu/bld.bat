@echo off

rem # Install anything that didn't get conda installed via pip.
rem # We need to turn pip index back on because Anaconda turns
rem # it off for some reason. Just pip install -r requirements.txt
rem # doesn't seem to work, tensorflow-gpu, jsonpickle, networkx,
rem # all get installed twice if we do this. pip doesn't see the
rem # conda install of the packages.

rem # Install the pip dependencies and their dependencies. Conda turns of
rem # pip index and dependencies by default so re-enable them. Had to figure
rem # this out myself, ughhh.
set PIP_NO_INDEX=False
set PIP_NO_DEPENDENCIES=False
set PIP_IGNORE_INSTALLED=False
pip install cattrs==1.0.0rc opencv-python-headless==3.4.1.15 PySide2==5.12.0 imgaug==0.3.0 qimage2ndarray==1.8 imgstore jsmin

rem # Use and update environment.yml call to install pip dependencies. This is slick.
rem # While environment.yml contains the non pip dependencies, the only thing left
rem # uninstalled should be the pip stuff because that is left out of meta.yml
rem conda env update -f=environment.yml

rem # Install requires setuptools-scm
pip install setuptools-scm

rem # Install sleap itself.
rem # NOTE: This is the recommended way to install packages
python setup.py install --single-version-externally-managed --record=record.txt
