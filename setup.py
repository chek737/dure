from setuptools import find_packages, setup


setup(
    name="dure",
    version="0.1.0",
    description="Resource-aware community LLM node bootstrapper",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    python_requires=">=3.10",
    package_dir={"": "src"},
    packages=find_packages("src"),
    entry_points={"console_scripts": ["dure=dure.cli:main"]},
)

