"""Fetch historical klines into data/*.csv (thin wrapper over ingestion_bybit CLI)."""
from tbb.marketdata.ingestion_bybit import _cli

if __name__ == "__main__":
    _cli()
