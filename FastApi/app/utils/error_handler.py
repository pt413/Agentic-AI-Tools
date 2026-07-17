# app/utils/error_handler.py
import traceback
from typing import Callable, Any, Optional
from sqlalchemy.exc import SQLAlchemyError, IntegrityError, OperationalError
from app.utils.logger import get_logger

_default_logger = get_logger("error-handler")

def safe_execute(operation: Callable[[], Any],
                 logger=None,
                 context: Optional[dict] = None) -> Any:
    """
    Execute operation() and catch expected DB/other errors.
    - logger: logger instance (if None use module default)
    - context: optional dict of contextual fields to log (entity type, id, etc.)
    Returns the operation return value, or None on handled exception.
    Note: this wrapper **does not** alter business logic; it only logs errors.
    """
    if logger is None:
        logger = _default_logger
    try:
        return operation()
    except IntegrityError as e:
        logger.error("IntegrityError | context=%s | error=%s", context, str(e))
        logger.debug(traceback.format_exc())
    except OperationalError as e:
        logger.error("OperationalError | context=%s | error=%s", context, str(e))
        logger.debug(traceback.format_exc())
    except SQLAlchemyError as e:
        logger.error("SQLAlchemyError | context=%s | error=%s", context, str(e))
        logger.debug(traceback.format_exc())
    except Exception as e:
        logger.error("UnexpectedError | context=%s | error=%s", context, str(e))
        logger.debug(traceback.format_exc())
    return None
