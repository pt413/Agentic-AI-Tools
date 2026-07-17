// Message buffer for batch processing
const axios = require('axios');

class MessageBuffer {
    constructor(batchSize = 100, flushIntervalMs = 5000, baseUrl = 'http://127.0.0.1:8000') {
        this.messages = [];
        this.batchSize = batchSize;
        this.flushIntervalMs = flushIntervalMs;
        this.lastFlush = Date.now();
        this.baseUrl = baseUrl;
        this.flushing = false;
        // Periodic flush to ensure messages are sent even if add() isn't called frequently
        this.timer = setInterval(() => {
            // call flush but don't block
            this.flush().catch(err => {
                console.error(JSON.stringify({ status: 'error', event: 'periodic_flush_failed', error: err.message }));
            });
        }, this.flushIntervalMs);
    }

    async add(message) {
        this.messages.push(message);

        // Check if we should flush based on size or time
        const now = Date.now();
        if (this.messages.length >= this.batchSize || (now - this.lastFlush) >= this.flushIntervalMs) {
            await this.flush();
        }
    }

    async flush() {
        if (this.flushing) return;
        if (this.messages.length === 0) return;

        this.flushing = true;
        try {
            const messagesToSend = [...this.messages];
            this.messages = [];
            this.lastFlush = Date.now();

            let retryCount = 0;
            const maxRetries = 3;
            let success = false;

            while (retryCount <= maxRetries && !success) {
            try {
                const response = await axios.post(
                    `${this.baseUrl}/connector/api/whatsapp/messages/bulk`,
                    messagesToSend,
                    {
                        headers: {
                            'Content-Type': 'application/json',
                            'Accept': 'application/json'
                        },
                        timeout: 30000 // 30 second timeout
                    }
                );

                console.log(JSON.stringify({
                    status: 'info',
                    event: 'messages_flushed',
                    count: messagesToSend.length,
                    response: response.data
                }));
                success = true;
            } catch (error) {
                retryCount++;
                const isFinal = retryCount > maxRetries;
                
                console.error(JSON.stringify({
                    status: 'error',
                    event: 'flush_failed',
                    count: messagesToSend.length,
                    retry: retryCount,
                    isFinal,
                    error: error.response?.data || error.message
                }));

                if (!isFinal) {
                    const delay = Math.pow(2, retryCount) * 1000;
                    await new Promise(r => setTimeout(r, delay));
                } else {
                    // Final retry failed, attempt single message fallback as last resort
                    for (const msg of messagesToSend) {
                        try {
                            await axios.post(
                                `${this.baseUrl}/connector/api/whatsapp/messages`,
                                msg,
                                {
                                    headers: {
                                        'Content-Type': 'application/json',
                                        'Accept': 'application/json'
                                    },
                                    timeout: 10000
                                }
                            );
                        } catch (e) {
                            console.error(JSON.stringify({
                                status: 'error',
                                event: 'single_message_fallback_failed',
                                message_id: msg.message_id,
                                error: e.response?.data || e.message
                            }));
                            // If even single message fails, we prepend back to buffer for next flush
                            // but only if it's a transient error. For now, we drop to avoid infinite loop of dead messages.
                        }
                    }
                }
            }
        }
        } finally {
            this.flushing = false;
        }
    }
    async stop() {
        if (this.timer) {
            clearInterval(this.timer);
            this.timer = null;
        }
        await this.flush();
    }
}

module.exports = MessageBuffer;