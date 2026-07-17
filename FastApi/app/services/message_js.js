const axios = require('axios');

const BACKEND_URL = process.env.BACKEND_URL || 'http://127.0.0.1:8000';

// Simple HTTP-based message model for FastAPI backend
class Message {
  static async updateOne(filter, update, options = {}) {
    try {
      // ... (code omitted for brevity in instruction, but should reflect the mapping)
      const messageData = {
        message_id: update.$set.message_id,
        admin_number: update.$set.admin_number,
        cx_number: update.$set.cx_number,
        content: update.$set.content,
        clean_content: update.$set.clean_content,
        timestamp: update.$set.timestamp,
        direction: update.$set.direction,
        device: update.$set.device,
        issent: update.$set.issent,
        isread: update.$set.isread,
        message_type: update.$set.message_type,
        remote_jid: update.$set.remote_jid,
      };

      // Make HTTP POST request to FastAPI backend
      const response = await axios.post(`${BACKEND_URL}/api/whatsapp/messages`, messageData, {
        headers: {
          'Content-Type': 'application/json'
        }
      });

      return response.data;
    } catch (error) {
      console.error('Error saving message to FastAPI:', error.message);
      throw error;
    }
  }

  static async findOne(filter) {
    try {
      // Make HTTP GET request to FastAPI backend
      const response = await axios.get(`${BACKEND_URL}/api/whatsapp/messages/${filter.admin_number}`, {
        params: {
          message_id: filter.message_id
        }
      });

      return response.data.messages.find(msg => msg.message_id === filter.message_id) || null;
    } catch (error) {
      console.error('Error fetching message from FastAPI:', error.message);
      return null;
    }
  }

  static async find(filter) {
    try {
      // Make HTTP GET request to FastAPI backend
      const response = await axios.get(`${BACKEND_URL}/api/whatsapp/messages/${filter.admin_number}`);

      return response.data.messages.filter(msg => {
        if (filter.timestamp && filter.timestamp.$gte) {
          return new Date(msg.timestamp) >= filter.timestamp.$gte;
        }
        return true;
      });
    } catch (error) {
      console.error('Error fetching messages from FastAPI:', error.message);
      return [];
    }
  }
}

module.exports = Message;
