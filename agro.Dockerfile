FROM webodm/nodeodx:latest
COPY opendm/photo.py /code/opendm/photo.py
COPY opendm/multispectral.py /code/opendm/multispectral.py
COPY stages/run_opensfm.py /code/stages/run_opensfm.py
COPY stages/mvstex.py /code/stages/mvstex.py
