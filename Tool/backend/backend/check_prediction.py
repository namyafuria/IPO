import sqlite3
from app.trajectory_predictions_store import get_latest_trajectory_prediction

conn = sqlite3.connect(
    "ipo_database.db"
)  # adjust filename if yours is named differently
conn.row_factory = sqlite3.Row

result = get_latest_trajectory_prediction(
    conn, "Manipal Health Enterprises"
)  # try the exact company name as stored in your DB
print(result)
