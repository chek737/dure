from setuptools import find_packages, setup

setup(
    name="saem",
    version="0.1.0",
    description="Saem RAG cluster: install-once, role-assign-from-head node package",
    packages=find_packages(exclude=["*.egg-info", "build", "dist"]),
    python_requires=">=3.9",
    install_requires=[
        "fastapi",
        "uvicorn",
        "httpx",
        "click",
        "pyyaml",
        "qdrant-client",
        "sentence-transformers",
    ],
    entry_points={"console_scripts": ["saem=saem.cli:main"]},
)
