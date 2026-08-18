"""
Run this after data_pipeline.py, whenever you're ready to wire up the
chatbot. Requires NVIDIA_API_KEY in .env.

    python build_vectors.py
"""
from src.vector.store import build_vectors

if __name__ == "__main__":
    build_vectors()
