# from datetime import datetime, timedelta
# from typing import List, Dict, Any, Optional
# from sqlalchemy.orm import Session
# from sqlalchemy import desc, func
# from sqlalchemy.exc import IntegrityError
# from app.model.message import Message
# import aiohttp
# import logging

# logger = logging.getLogger(__name__)

# class WhatsAppSyncService: 
#     def __init__(self, db: Session):
#         self.db = db
    
#     async def get_admins(self): 
#         try:
#             # Get distinct admin numbers from messages
#             admin_numbers = self.db.query(Message.admin_number).distinct().all()
#             admin_numbers = [num[0] for num in admin_numbers if num[0]]
            
#             admins_data = []
#             for admin_number in admin_numbers:
#                 # Get the most recent message for this admin
#                 last_message = self.db.query(Message).filter(
#                     Message.admin_number == admin_number
#                 ).order_by(desc(Message.timestamp)).first()
                
#                 if last_message:
#                     # Calculate sync status based on last sync time
#                     last_sync = last_message.last_sync or last_message.timestamp
#                     time_since_last_sync = datetime.utcnow() - last_sync
                    
#                     # Check WhatsApp client status
#                     client_status = "unknown"
#                     try:
#                         async with aiohttp.ClientSession() as session:
#                             status_url = f'http://localhost:5000/status/{admin_number}'
#                             async with session.get(status_url) as response:
#                                 if response.status == 200:
#                                     data = await response.json()
#                                     client_status = data.get('status', 'unknown')
#                     except Exception as e:
#                         logger.warning(f"Could not get WhatsApp client status for {admin_number}: {str(e)}")
                    
#                     # Determine sync status based on both last sync time and client status
#                     if client_status == 'ready' and time_since_last_sync < timedelta(minutes=5):
#                         sync_status = "online"
#                     elif client_status == 'qr':
#                         sync_status = "needs_qr"
#                     elif client_status == 'not_started':
#                         sync_status = "offline"
#                     elif time_since_last_sync < timedelta(hours=1):
#                         sync_status = "online"
#                     else:
#                         sync_status = "offline"
                    
#                     # Count total messages for this admin
#                     message_count = self.db.query(Message).filter(
#                         Message.admin_number == admin_number
#                     ).count()
                    
#                     content = last_message.content or ""
#                     admins_data.append({
#                         "admin_number": admin_number,
#                         "last_sync": last_sync.isoformat(),
#                         "sync_status": sync_status,
#                         "client_status": client_status,
#                         "message_count": message_count,
#                         "last_message": content[:100] + ('...' if len(content) > 100 else ''),
#                         "qr_url": f"http://localhost:5000/status/{admin_number}" if client_status == 'qr' else None
#                     })
            
#             return admins_data
            
#         except Exception as e:
#             logger.error(f"Error fetching admins: {str(e)}")
#             raise
    
#     async def get_messages(self, admin_number: str, page: int = 1, limit: int = 50, 
#                           direction: Optional[str] = None, start_date: Optional[datetime] = None,
#                           end_date: Optional[datetime] = None):
#         try:
#             skip = (page - 1) * limit
            
#             # Build query with optional filters
#             query = self.db.query(Message).filter(Message.admin_number == admin_number)
            
#             # Add direction filter if provided
#             if direction in ['incoming', 'outgoing']:
#                 query = query.filter(Message.direction == direction)
                
#             # Add date filter if provided
#             if start_date:
#                 query = query.filter(Message.timestamp >= start_date)
#             if end_date:
#                 query = query.filter(Message.timestamp <= end_date)
            
#             total = query.count()
#             messages = query.order_by(desc(Message.timestamp)).offset(skip).limit(limit).all()
            
#             return {
#                 "messages": [{
#                     "id": msg.id,
#                     "admin_number": msg.admin_number,
#                     "content": msg.content,
#                     "clean_content": msg.clean_content,
#                     "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
#                     "last_sync": msg.last_sync.isoformat() if msg.last_sync else None,
#                     "direction": msg.direction,
#                     "isread": msg.isread,
#                     "issent": msg.issent,
#                     "media": getattr(msg, "media", None),
#                     "message_type": msg.message_type,
#                     "cx_number": msg.cx_number,
#                     "device": msg.device
#                 } for msg in messages],
#                 "page": page,
#                 "limit": limit,
#                 "total": total,
#                 "pages": (total + limit - 1) // limit
#             }
        
#         except Exception as e:
#             logger.error(f"Error fetching messages for {admin_number}: {str(e)}")
#             raise
    
#     async def sync_messages(self, admin_number: str):
#         try:
#             # Check if the admin exists in our database
#             admin_exists = self.db.query(Message).filter(
#                 Message.admin_number == admin_number
#             ).first()
            
#             # If no messages exist for this admin yet, initialize the WhatsApp client
#             if not admin_exists:
#                 try:
#                     # Start the WhatsApp client for this number
#                     async with aiohttp.ClientSession() as session:
#                         start_url = f'http://localhost:5000/start/{admin_number}'
#                         async with session.post(start_url) as response:
#                             if response.status != 200:
#                                 data = await response.json()
#                                 if not data.get('success'):
#                                     return {
#                                         "status": "pending",
#                                         "message": "Initializing WhatsApp client. Please scan the QR code when ready.",
#                                         "qr_url": f"http://localhost:5000/status/{admin_number}"
#                                     }
#                 except Exception as e:
#                     logger.error(f"Error initializing WhatsApp client: {str(e)}")
#                     raise Exception("Failed to initialize WhatsApp client")
            
#             # Get the last sync time
#             last_message = self.db.query(Message).filter(
#                 Message.admin_number == admin_number
#             ).order_by(desc(Message.last_sync)).first()
            
#             if last_message and last_message.last_sync:
#                 last_sync_time = last_message.last_sync
#             else:
#                 # Default to 1 hour ago if no previous sync
#                 last_sync_time = datetime.utcnow() - timedelta(hours=1)
            
#             # Fetch new messages (this will also trigger the whatsapp-web.js service to check for new messages)
#             new_messages = await self.fetch_whatsapp_messages(admin_number, last_sync_time)
            
#             # Save any new messages to the database
#             saved_count = await self.save_messages_to_db(new_messages)
            
#             # Update last sync time for this admin
#             # self.db.query(Message).filter(
#             #     Message.admin_number == admin_number
#             # ).update({Message.last_sync: datetime.utcnow()})
#             # self.db.commit()
            
#             # Get total message count
#             total_messages = self.db.query(Message).filter(
#                 Message.admin_number == admin_number
#             ).count()
            
#             return {
#                 "status": "success",
#                 "new_messages": saved_count,
#                 "total_messages": total_messages,
#                 "last_sync": datetime.utcnow().isoformat()
#             }
            
#         except Exception as e:
#             logger.error(f"Error syncing messages for {admin_number}: {str(e)}")
#             raise
    
#     async def get_sync_status(self):
#         try:
#             # Get all admin numbers
#             admin_numbers = self.db.query(Message.admin_number).distinct().all()
#             admin_numbers = [num[0] for num in admin_numbers if num[0]]
            
#             status_data = []
#             for admin_number in admin_numbers:
#                 # Get the last message for this admin
#                 last_message = self.db.query(Message).filter(
#                     Message.admin_number == admin_number
#                 ).order_by(desc(Message.timestamp)).first()
                
#                 if last_message:
#                     last_sync = last_message.last_sync or last_message.timestamp
#                     time_diff = datetime.utcnow() - last_sync
                    
#                     status_data.append({
#                         "admin_number": admin_number,
#                         "last_sync": last_sync.isoformat(),
#                         "status": "online" if time_diff < timedelta(hours=24) else "offline",
#                         "hours_since_sync": round(time_diff.total_seconds() / 3600, 2),
#                         "message_count": self.db.query(Message).filter(
#                             Message.admin_number == admin_number
#                         ).count()
#                     })
            
#             return status_data
        
#         except Exception as e:
#             logger.error(f"Error fetching sync status: {str(e)}")
#             raise
    
#     async def add_message(self, message_data: Dict[str, Any]):
#         try:
#             # Validate required fields
#             required_fields = ["admin_number", "content", "timestamp"]
#             for field in required_fields:
#                 if field not in message_data:
#                     raise ValueError(f"Missing required field: {field}")
            
#             # Set message_id if not provided
#             message_id = message_data.get("message_id")
#             if not message_id:
#                 message_id = f"MSG_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"

#             existing = self.db.query(Message).filter(Message.message_id == message_id).first()

#             parsed_ts = datetime.fromisoformat(message_data["timestamp"].replace('Z', '+00:00'))

#             if existing:
#                 existing.admin_number = message_data.get("admin_number", existing.admin_number)
#                 existing.cx_number = message_data.get("cx_number", existing.cx_number)
#                 existing.direction = message_data.get("direction", existing.direction)
#                 existing.content = message_data.get("content", existing.content)
#                 existing.clean_content = message_data.get("clean_content", existing.clean_content)
#                 existing.timestamp = parsed_ts
#                 # existing.last_sync = datetime.utcnow()
#                 existing.device = message_data.get("device", existing.device)
#                 existing.isread = message_data.get("isread", existing.isread)
#                 existing.issent = message_data.get("issent", existing.issent)
#                 existing.message_type = message_data.get("message_type", existing.message_type)
#                 existing.remote_jid = message_data.get("remote_jid", existing.remote_jid)

#                 try:
#                     self.db.commit()
#                 except IntegrityError:
#                     self.db.rollback()
#                 return {
#                     "message": "Message already exists (updated)",
#                     "id": existing.id
#                 }

#             # Create new message
#             message = Message(
#                 admin_number=message_data["admin_number"],
#                 content=message_data["content"],
#                 clean_content=message_data.get("clean_content", message_data["content"]),
#                 timestamp=parsed_ts,
#                 last_sync=datetime.utcnow(),
#                 direction=message_data.get("direction", "incoming"),
#                 device=message_data.get("device", "baileys"),
#                 isread=message_data.get("isread", False),
#                 issent=message_data.get("issent", False),
#                 message_type=message_data.get("message_type", "text"),
#                 cx_number=message_data.get("cx_number", ""),
#                 remote_jid=message_data.get("remote_jid", ""),
#                 message_id=message_id
#             )

#             self.db.add(message)
#             try:
#                 self.db.commit()
#             except IntegrityError:
#                 # Another worker inserted concurrently
#                 self.db.rollback()
#                 existing = self.db.query(Message).filter(Message.message_id == message_id).first()
#                 return {
#                     "message": "Message already exists (race)",
#                     "id": existing.id if existing else None
#                 }
#             self.db.refresh(message)

#             return {
#                 "message": "Message added successfully",
#                 "id": message.id
#             }
        
#         except Exception as e:
#             self.db.rollback()
#             logger.error(f"Error adding message: {str(e)}")
#             raise
    
#     async def fetch_whatsapp_messages(self, admin_number: str, since: datetime) -> List[Dict[str, Any]]:
#         """
#         Fetch WhatsApp messages for a specific admin number since a given timestamp
#         using the existing whatsapp-web.js service.
#         """
#         try:
#             # Get messages from the local database first
#             existing_messages = self.db.query(Message).filter(
#                 Message.admin_number == admin_number,
#                 Message.timestamp >= since
#             ).order_by(Message.timestamp).all()
            
#             # Convert to list of dicts
#             messages = []
#             for msg in existing_messages:
#                 messages.append({
#                     'admin_number': msg.admin_number,
#                     'content': msg.content,
#                     'clean_content': msg.clean_content,
#                     'timestamp': msg.timestamp,
#                     'direction': msg.direction,
#                     'device': msg.device,
#                     'isread': msg.isread,
#                     'issent': msg.issent,
#                     'media': getattr(msg, 'media', None),
#                     'message_type': msg.message_type,
#                     'message_id': msg.message_id,
#                     'cx_number': msg.cx_number,
#                     'remote_jid': msg.remote_jid,
#                     'participant': getattr(msg, 'participant', None)
#                 })
            
#             # Check for new messages via the whatsapp-web.js service
#             try:
#                 async with aiohttp.ClientSession() as session:
#                     service_url = f'http://localhost:5000/status/{admin_number}'
#                     async with session.get(service_url) as response:
#                         if response.status == 200:
#                             status_data = await response.json()
#                             if status_data.get('status') == 'ready':
#                                 # The service is ready, we can fetch new messages
#                                 # The actual messages are already being saved to the database 
#                                 # by the whatsapp-web.js service via the log_message function
#                                 pass
#             except Exception as e:
#                 logger.error(f"Error checking WhatsApp service status for {admin_number}: {str(e)}")
                    
#             return messages
            
#         except Exception as e:
#             logger.error(f"Error fetching WhatsApp messages for {admin_number}: {str(e)}")
#             # Return empty list if there was an error
#             return []
    
#     async def save_messages_to_db(self, messages: List[Dict[str, Any]]) -> int:
#         """Save fetched WhatsApp messages to PostgreSQL"""
#         saved_count = 0
        
#         for msg_data in messages:
#             # Check if message already exists
#             existing_msg = self.db.query(Message).filter(
#                 Message.message_id == msg_data.get("message_id")
#             ).first()
            
#             if existing_msg:
#                 continue
                
#             # Create new message
#             message = Message(
#                 admin_number=msg_data["admin_number"],
#                 content=msg_data["content"],
#                 clean_content=msg_data.get("clean_content", msg_data["content"]),
#                 timestamp=msg_data["timestamp"],
#                 last_sync=datetime.utcnow(),
#                 direction=msg_data.get("direction", "incoming"),
#                 device=msg_data.get("device", "baileys"),
#                 isread=msg_data.get("isread", False),
#                 issent=msg_data.get("issent", False),
#                 message_type=msg_data.get("message_type", "text"),
#                 message_id=msg_data.get("message_id"),
#                 cx_number=msg_data.get("cx_number", ""),
#                 remote_jid=msg_data.get("remote_jid", "")
#             )
            
#             self.db.add(message)
#             saved_count += 1
        
#         if saved_count > 0:
#             self.db.commit()
        
#         return saved_count