import pandas as pd
import numpy as np
import sqlite3
import threading

class DataStore:
    def __init__(self, db_path="market_data.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self.init_db()

    def init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS price_history (
                    timestamp REAL PRIMARY KEY,
                    pool_price REAL,
                    external_price REAL,
                    tick INTEGER,
                    diff REAL,
                    diff_ratio REAL
                )
            """)
            try:
                conn.execute("ALTER TABLE price_history ADD COLUMN diff_ratio REAL")
            except sqlite3.OperationalError:
                pass

    def append_data(self, data_dict):
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                diff_ratio = data_dict.get('diff_ratio', 0)
                conn.execute("INSERT INTO price_history VALUES (?, ?, ?, ?, ?, ?)", 
                            (data_dict['timestamp'], data_dict['pool_price'], 
                             data_dict['external_price'], data_dict['tick'], 
                             data_dict['diff'], diff_ratio))

    def get_volatility(self, window=20):
        """Calculate volatility from recent price data"""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                df = pd.read_sql_query(f"SELECT external_price FROM price_history ORDER BY timestamp DESC LIMIT {window}", conn)
                
                if len(df) < 2:
                    return 0.01
                
                returns = np.log(df['external_price'] / df['external_price'].shift(1)).dropna()
                volatility = returns.std()
                
                return volatility

    def get_moving_average_diff(self, window=10):
        """Calculate moving average of price deviation"""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                df = pd.read_sql_query(f"SELECT diff FROM price_history ORDER BY timestamp DESC LIMIT {window}", conn)
                return df['diff'].mean()

store = DataStore()