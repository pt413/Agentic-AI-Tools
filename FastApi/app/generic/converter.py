
import datetime
from bson import ObjectId

def json_converter(o):
    if isinstance(o, datetime.datetime):
        return o.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(o, ObjectId):
        return str(o)
    return str(o)
