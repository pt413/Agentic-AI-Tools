from sqlalchemy import create_engine, text
import sys

# 🔐 CONFIGURE THESE
USERNAME = "readonly_admin"
PASSWORD = "dbra#pasS"


DATABASE = "rms_db"
HOST = "127.0.0.1"
PORT = 3307

try:
    engine = create_engine(
        f"mysql+pymysql://{USERNAME}:{PASSWORD}@{HOST}:{PORT}/{DATABASE}",
        pool_pre_ping=True,
        pool_recycle=3600,
    )

    with engine.connect() as conn:
        print("✅ Connected to MySQL RDS")

        # Test 1: Server time
        result = conn.execute(text("SELECT NOW()"))
        print("Server Time:", result.scalar())

        # Test 2: Current user
        result = conn.execute(text("SELECT USER()"))
        print("Connected User:", result.scalar())

        # Test 3: List first 5 tables
        result = conn.execute(text("SHOW TABLES"))
        tables = [row[0] for row in result.fetchall()]
        print("Tables Found:", tables[:5])

except Exception as e:
    print("❌ Connection Failed")
    print(e)
    sys.exit(1)