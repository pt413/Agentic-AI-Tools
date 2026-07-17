
import redis.asyncio as redis
from typing import Optional, Dict, Any
import json
from datetime import datetime

class SessionManager:
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
    
    async def get_previous_query(self, session_key: str) -> str:
        
        try:
            data = await self.redis.get(f"session:{session_key}")
            if data:
                session_data = json.loads(data)
                return session_data.get('previous_query', '')
            return ''
        except Exception as e:
            print(f"Error getting session: {e}")
            return ''
    
    async def update_session(self, session_key: str, current_query: str, max_sessions_per_user: int = 50):
       
        try:
            session_data = {
                'previous_query': current_query,
                'last_updated': datetime.utcnow().isoformat(),
                'query_count': 1
            }
            
       
            existing = await self.redis.get(f"session:{session_key}")
            if existing:
                existing_data = json.loads(existing)
                session_data['query_count'] = existing_data.get('query_count', 0) + 1
            
           
            await self.redis.setex(
                f"session:{session_key}", 
                86400,  # 24 hours
                json.dumps(session_data)
            )
            
           
            await self._cleanup_old_sessions(session_key, max_sessions_per_user)
            
        except Exception as e:
            print(f"Error updating session: {e}")
    
    async def _cleanup_old_sessions(self, current_session_key: str, max_sessions: int):
        """Cleanup old sessions for the same user"""
        try:
            if 'user:' in current_session_key:
                user_id = current_session_key.split(':')[1]
                pattern = f"session:user:{user_id}:*"
                
               
                keys = await self.redis.keys(pattern)
                if len(keys) > max_sessions:
                    
                    sessions_data = []
                    for key in keys:
                        data = await self.redis.get(key)
                        if data:
                            session_data = json.loads(data)
                            sessions_data.append((key, session_data.get('last_updated', '')))
                    
                  
                    sessions_data.sort(key=lambda x: x[1])
                    keys_to_delete = [key for key, _ in sessions_data[:len(keys) - max_sessions]]
                    
                    for key in keys_to_delete:
                        await self.redis.delete(key)
                        
        except Exception as e:
            print(f"Error cleaning up sessions: {e}")

class SmartCacheManager:
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
    
    async def get_cached_response(self, cache_key: str) -> Optional[Dict]:
        """Get cached response with validation"""
        try:
            cached_data = await self.redis.get(cache_key)
            if cached_data:
                if isinstance(cached_data, bytes):
                    cached_data = cached_data.decode('utf-8')
                return json.loads(cached_data)
            return None
        except Exception as e:
            print(f"Cache get error: {e}")
            return None
    
    async def set_cached_response(self, cache_key: str, data: Dict, ttl: int = 3600):
        """Set cached response with size validation"""
        try:
            result_json = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
            await self.redis.setex(cache_key, ttl, result_json)
        except Exception as e:
            print(f"Cache set error: {e}")
    
    async def cleanup_old_cache(self, pattern: str = "cache:*", max_age_hours: int = 24):
        """Cleanup cache entries older than specified hours"""
        try:
            keys = await self.redis.keys(pattern)
            for key in keys:
                ttl = await self.redis.ttl(key)
                if ttl < 0 or ttl > 86400 * 7:  # More than 7 days
                    await self.redis.delete(key)
        except Exception as e:
            print(f"Cache cleanup error: {e}")

class RateLimiter:
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
    
    async def check_rate_limit(self, user_id: int, max_requests: int = 100, window_seconds: int = 3600):
        """Check if user has exceeded rate limit"""
        key = f"rate_limit:user:{user_id}"
        try:
            current = await self.redis.get(key)
            if current:
                count = int(current)
                if count >= max_requests:
                    return False
                await self.redis.incr(key)
            else:
                await self.redis.setex(key, window_seconds, 1)
            return True
        except Exception as e:
            print(f"Rate limit error: {e}")
            return True
    
    async def get_usage_stats(self, user_id: int):
        """Get user's API usage statistics"""
        key = f"rate_limit:user:{user_id}"
        try:
            count = await self.redis.get(key)
            return int(count) if count else 0
        except:
            return 0