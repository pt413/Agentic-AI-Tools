import importlib
import pkgutil
import os
import subprocess
from app.db.database import engine, Base

def import_all_models():
    """Dynamically import all models under app/model/."""
    from app import model
    package_path = model.__path__
    for _, module_name, _ in pkgutil.iter_modules(package_path):
        importlib.import_module(f"app.model.{module_name}")
    
    try:
        import app.routes.files_rag
    except ImportError as e:
        print(f"Warning: Could not import app.routes.files_rag: {e}")

def create_all_tables():
    """Create all tables once after importing models."""
    import_all_models()
    Base.metadata.create_all(bind=engine, checkfirst=True)
    print("Done: All tables created successfully!")

# def create_all_tables():
#     """Create all tables once after importing models."""
#     import_all_models()
#     from app.db.database import engine, Base
#     with engine.connect() as conn:
#         conn.execution_options(isolation_level="AUTOCOMMIT")  # important for DDL
#         Base.metadata.create_all(bind=conn, checkfirst=True)
#     print("✅ All tables created successfully!")


def has_model_changes():
    """Check if any model file has changed since last run."""
    model_dir = os.path.join(os.path.dirname(__file__), "../model")
    latest_model_time = max(
        os.path.getmtime(os.path.join(model_dir, f))
        for f in os.listdir(model_dir)
        if f.endswith(".py")
    )
    flag_time = os.path.getmtime(".tables_created") if os.path.exists(".tables_created") else 0
    return latest_model_time > flag_time

def run_alembic_upgrade():
    """Run Alembic migrations to apply schema changes."""
    print("Running Alembic migrations for schema changes...")
    result = subprocess.run(["alembic", "upgrade", "head"], capture_output=True, text=True)
    if result.returncode != 0:
        print("Error: Alembic migration failed:\n", result.stderr)
        raise RuntimeError("Alembic migration failed")
    print("Done: Alembic migrations applied successfully.")

def create_tables_once():
    FLAG_FILE = ".tables_created"
    
    if os.path.exists(FLAG_FILE) and not has_model_changes():
        print("Tables already created - skipping creation/migration.")
        return
    
    print("Creating all tables in PostgreSQL...")
    create_all_tables()
    
    # Apply migrations if models changed
    if has_model_changes():
        run_alembic_upgrade()
    
    # Update flag file timestamp
    open(FLAG_FILE, "w").close()

if __name__ == "__main__":
    create_tables_once()
