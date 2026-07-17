import os
from dotenv import load_dotenv

load_dotenv()

AWS_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY")
AWS_SECRET_KEY = os.getenv("AWS_SECRET_KEY")
S3_BUCKET      = os.getenv("S3_BUCKET")
REGION         = os.getenv("REGION")
DATABASE_URL   = os.getenv("DATABASE_URL")