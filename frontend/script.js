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

    const API_BASE_URL = 'https://tegailawyer-production.up.railway.app/api';

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

        try {
            const response = await fetch(`${API_BASE_URL}/ask`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ 
                    message: messageText, 
                    session_id: sessionId 
                }),
            });

            hideLoading();

            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }

            const reader = response.body.getReader();
            const decoder = new TextDecoder('utf-8');
            let botResponse = '';
            let botMessageDiv = null;

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                
                const chunk = decoder.decode(value, { stream: true });
                botResponse += chunk;
                
                // Create or update bot message
                if (!botMessageDiv) {
                    botMessageDiv = addMessage('bot', botResponse);
                } else {
                    updateBotMessage(botMessageDiv, botResponse);
                }
            }

        } catch (error) {
            hideLoading();
            console.error('Error sending message:', error);
            addMessage('bot', `
                <div style="color: #e74c3c; padding: 1rem; background: #fdf2f2; border-radius: 10px; border-left: 4px solid #e74c3c;">
                    <strong>Произошла ошибка</strong><br>
                    Извините, не удалось получить ответ. Пожалуйста, попробуйте еще раз или проверьте подключение к интернету.
                </div>
            `);
        }
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


