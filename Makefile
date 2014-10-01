PIP_INDEX_URL := https://pypi.python.org/simple/

.DELETE_ON_ERROR:

all: sdist bdist_wheel topology_builder

sdist:
	python setup.py sdist

bdist_wheel:
	python setup.py bdist_wheel

test:
	tox -c tox.ini -i $(PIP_INDEX_URL)

topology_builder:
	make -C topology_builder/

clean:
	rm -rf build/ dist/ pyleus.egg-info/ .tox/
	find . -name '*.pyc' -delete
	make -C topology_builder/ clean

docs:
	tox -e docs

.PHONY: all sdist bdist_wheel test clean docs
