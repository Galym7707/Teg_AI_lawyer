document.addEventListener('DOMContentLoaded', () => {
    const chatHistory = document.getElementById('chat-history');
    const userInput = document.getElementById('user-input');
    const sendButton = document.getElementById('send-button');
    const loadingOverlay = document.getElementById('loading-overlay');

    let sessionId = localStorage.getItem('kazLegalBotSessionId');
    if (!sessionId) {
        sessionId = 'session_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);
        localStorage.setItem('kazLegalBotSessionId', sessionId);
    }

    const API_BASE = '/api'; // Netlify proxy -> бэкенд

    // Auto-resize textarea
    function autoResize(textarea) {
        textarea.style.height = 'auto';
        textarea.style.height = Math.min(textarea.scrollHeight, 120) + 'px';
    }

    userInput.addEventListener('input', () => autoResize(userInput));

    function showLoading() {
        loadingOverlay.classList.add('show');
    }

    function hideLoading() {
        loadingOverlay.classList.remove('show');
    }

    function addMessage(sender, text, isStreaming = false) {
        // Remove welcome message if it exists
        const welcomeMessage = chatHistory.querySelector('.welcome-message');
        if (welcomeMessage) {
            welcomeMessage.remove();
        }

        const messageDiv = document.createElement('div');
        messageDiv.classList.add('message', sender);
        
        if (sender === 'bot') {
            messageDiv.innerHTML = text; // Use innerHTML to render HTML from bot
        } else {
            messageDiv.textContent = text; // Use textContent for user messages for security
        }
        
        chatHistory.appendChild(messageDiv);
        chatHistory.scrollTop = chatHistory.scrollHeight;
        
        return messageDiv;
    }

    function updateBotMessage(messageDiv, text) {
        messageDiv.innerHTML = text;
        chatHistory.scrollTop = chatHistory.scrollHeight;
    }

    async function sendMessage(message = null) {
        const messageText = message || userInput.value.trim();
        if (messageText === '') return;

        // Add user message
        addMessage('user', messageText);
        
        // Clear input and reset height
        if (!message) {
            userInput.value = '';
            autoResize(userInput);
        }

        // Show loading
        showLoading();

        const url = `${API_BASE}/ask`;
        const payload = { question: messageText.trim() };

        // Жирный дебаг запроса
        console.debug('[sendMessage] POST', url, {
            headers: { 'Content-Type': 'application/json' },
            body: payload
        });

        let res;
        try {
            res = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify(payload)
            });
        } catch (netErr) {
            console.error('[sendMessage] Network error:', netErr);
            hideLoading();
            addMessage('bot', `
                <div style="color: #e74c3c; padding: 1rem; background: #fdf2f2; border-radius: 10px; border-left: 4px solid #e74c3c;">
                    <strong>Ошибка сети</strong><br>
                    Сеть недоступна или бэкенд не отвечает.
                </div>
            `);
            return;
        }

        const rawText = await res.text();
        console.debug('[sendMessage] Response status:', res.status);
        console.debug('[sendMessage] Raw response body:', rawText);

        // Пытаемся распарсить JSON (даже если статус не 2xx)
        let data = null;
        try {
            data = JSON.parse(rawText);
        } catch {
            // оставим rawText в логе
        }

        if (!res.ok) {
            const msg = data?.error?.message || `HTTP ${res.status}`;
            const code = data?.error?.code || 'UNKNOWN';
            console.error('[sendMessage] Backend error:', { code, msg, debug: data?.debug });
            hideLoading();
            addMessage('bot', `
                <div style="color: #e74c3c; padding: 1rem; background: #fdf2f2; border-radius: 10px; border-left: 4px solid #e74c3c;">
                    <strong>Ошибка сервера</strong><br>
                    ${code}. ${msg}
                </div>
            `);
            return;
        }

        if (!data?.ok) {
            console.error('[sendMessage] Logical error:', data);
            hideLoading();
            addMessage('bot', `
                <div style="color: #e74c3c; padding: 1rem; background: #fdf2f2; border-radius: 10px; border-left: 4px solid #e74c3c;">
                    <strong>Ошибка</strong><br>
                    ${data?.error?.message || 'Не удалось получить ответ.'}
                </div>
            `);
            return;
        }

        hideLoading();
        // У вас рендер HTML
        addMessage('bot', data.answer_html);
    }

    // Global function for question tags
    window.askQuestion = function(question) {
        sendMessage(question);
    };

    // Event listeners
    sendButton.addEventListener('click', () => sendMessage());
    
    userInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    // Initialize without loading history (MongoDB disabled)
    console.log('Kaz Legal Bot initialized with session:', sessionId);
    
    // Focus on input
    userInput.focus();
});


