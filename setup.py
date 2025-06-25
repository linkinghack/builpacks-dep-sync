from setuptools import setup, find_packages

setup(
    name='bpsync',
    version='0.1.0',
    description='Buildpack dependency sync tool with registry upload',
    author='linkinghack',
    author_email='linking@linkinghack.com',
    packages=find_packages(),
    py_modules=['sync_dependencies'],
    install_requires=[
        'requests',
        'toml',
    ],
    entry_points={
        'console_scripts': [
            'bpsync = sync_dependencies:main',
        ],
    },
    python_requires='>=3.6',
)
