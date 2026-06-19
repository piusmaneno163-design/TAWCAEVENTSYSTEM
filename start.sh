#!/usr/bin/env sh
streamlit run app.py --server.port ${PORT:-8501} --server.address 0.0.0.0
