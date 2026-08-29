"""Dev/prod launcher for the API (uvicorn tbb.api.app)."""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("tbb.api.app:app", host="0.0.0.0", port=8000)
