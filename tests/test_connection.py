import requests
print(requests.get("https://api.bybit.com/v5/market/time").json())
