import redis
import json
import asyncio
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
import hashlib

class BookingCacheManager:
    def __init__(self):
        self.redis_client = redis.Redis(
            host='localhost',
            port=6379,
            db=0,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5
        )
        self.cache_ttl = 3600  # 1 hour
    
    def _generate_key(self, booking_id: str, data_type: str) -> str:
        """Generate Redis key for booking data"""
        return f"booking:{booking_id}:{data_type}"
    
    async def get_cached_data(self, booking_id: str, data_type: str) -> Optional[Any]:
        """Get cached data for booking"""
        try:
            key = self._generate_key(booking_id, data_type)
            cached = self.redis_client.get(key)
            if cached:
                print(f"✅ Cache HIT for {data_type} - booking {booking_id}")
                return json.loads(cached)
            print(f"❌ Cache MISS for {data_type} - booking {booking_id}")
            return None
        except Exception as e:
            print(f"⚠️ Cache error for {data_type}: {e}")
            return None
    
    async def set_cached_data(self, booking_id: str, data_type: str, data: Any):
        """Set cached data for booking"""
        try:
            key = self._generate_key(booking_id, data_type)
            self.redis_client.setex(
                key, 
                self.cache_ttl, 
                json.dumps(data, default=str)
            )
            print(f"✅ Cached {data_type} for booking {booking_id}")
        except Exception as e:
            print(f"⚠️ Cache set error for {data_type}: {e}")
    
    async def cache_emails_with_categories(self, booking_id: str, communications_data: Dict):
        """Categorize and cache emails"""
        if not communications_data or 'communication' not in communications_data:
            return
        
        emails = communications_data['communication']
        if not isinstance(emails, list):
            return
        
        categorized_emails = []
        
        for email in emails:
            subject = email.get('subject', '').lower()
            body = str(email.get('body', '')).lower()
            
            # Categorize emails
            categories = []
            if any(word in subject or word in body for word in ['cancel', 'cancellation', 'refund']):
                categories.append('cancellation')
            if any(word in subject or word in body for word in ['extend', 'extension', 'renew']):
                categories.append('extension')
            if any(word in subject or word in body for word in ['payment', 'invoice', 'due']):
                categories.append('payment')
            if any(word in subject or word in body for word in ['complaint', 'issue', 'problem']):
                categories.append('complaint')
            
            email['categories'] = categories if categories else ['general']
            categorized_emails.append(email)
        
        await self.set_cached_data(booking_id, "emails", categorized_emails)
    
    async def get_emails_by_category(self, booking_id: str, category: str) -> List[Dict]:
        """Get emails by category"""
        all_emails = await self.get_cached_data(booking_id, "emails")
        if not all_emails:
            return []
        
        filtered_emails = [
            email for email in all_emails 
            if category in email.get('categories', [])
        ]
        print(f"✅ Retrieved {len(filtered_emails)} {category} emails from cache")
        return filtered_emails

# Global cache instance
cache_manager = BookingCacheManager()