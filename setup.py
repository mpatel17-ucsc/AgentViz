from setuptools import setup, find_packages

setup(
    name="agentviz",
    version="0.1.0",
    packages=find_packages(),
    entry_points={
        "console_scripts": [
            "agentviz=agentviz.cli:main",
        ],
    },
    install_requires=[
        "requests",
        "python-socketio[client]",
        "psutil",
        "watchdog"
    ],
)
