from setuptools import setup

setup(
    name="cbpi4-ventil",
    version="0.1.0",
    description="CraftBeerPi4: Brauwasser mit Magnetventil und Durchflussmesser abmessen",
    author="Timm Kück",
    url="https://github.com/timmkueck/cbpi4-ventil",
    packages=["cbpi4_ventil"],
    install_requires=["lgpio>=0.2.2"],
    extras_require={"test": ["pytest"]},
    python_requires=">=3.9",
)
