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

    /* =========   API BASE   ========= */
    /* В проде уходим напрямую напрямую на Railway, чтобы не ловить 504 от Netlify-прокси. */
    const PROD_BACKEND = "https://tegailawyer-production.up.railway.app/api";
    const API_BASE = (location.hostname.endsWith("netlify.app") || location.hostname.endsWith("vercel.app"))
      ? PROD_BACKEND
      : "/api";

    /* =========   FETCH с максимальным дебагом   ========= */
    async function apiFetch(path, options = {}, retries = 1) {
      const url = `${API_BASE}${path}`;
      const opts = {
        method: "GET",
        credentials: "include",
        ...options,
        headers: {
          "Content-Type": "application/json",
          ...(options.headers || {}),
        },
      };

      // превью тела
      let bodyPreview = "";
      try {
        if (opts.body && typeof opts.body !== "string") {
          bodyPreview = JSON.stringify(opts.body).slice(0, 500);
          opts.body = JSON.stringify(opts.body);
        } else if (typeof opts.body === "string") {
          bodyPreview = opts.body.slice(0, 500);
        }
      } catch (_) {}

      console.debug("→ fetch", url, { ...opts, body: bodyPreview });

      for (let i = 0; i <= retries; i++) {
        try {
          const res = await fetch(url, opts);
          const ct = res.headers.get("content-type") || "";
          const text = await res.clone().text();
          console.debug("←", res.status, "CT:", ct, "BodyPreview:", text.slice(0, 400));

          if (!res.ok) {
            throw new Error(`HTTP ${res.status}: ${text || res.statusText}`);
          }

          // Вернем Response-подобную сущность c уже считанным текстом
          return {
            ok: true,
            status: res.status,
            headers: res.headers,
            text: async () => text,
            json: async () => (ct.includes("application/json") ? JSON.parse(text || "{}") : { raw: text }),
          };
        } catch (e) {
          console.warn(`Retry ${i + 1}/${retries} for ${url}:`, e.message);
          if (i < retries) {
            await new Promise(r => setTimeout(r, 800 * (i + 1)));
          } else {
            throw e;
          }
        }
      }
    }

    /* Вызовы:
       await apiFetch("/ask", { method: "POST", body: { question: "..." } });
       await apiFetch("/health");
    */

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
            const res = await apiFetch("/ask", { 
                method: "POST", 
                body: { question: messageText.trim() } 
            });
            
            const data = await res.json();
            
            if (!data?.ok) {
                console.error('[sendMessage] Logical error:', data);
                addMessage('bot', `
                    <div style="color: #e74c3c; padding: 1rem; background: #fdf2f2; border-radius: 10px; border-left: 4px solid #e74c3c;">
                        <strong>Ошибка</strong><br>
                        ${data?.error?.message || 'Не удалось получить ответ.'}
                    </div>
                `);
                return;
            }

            // У вас рендер HTML
            addMessage('bot', data.answer_html);
        } catch (error) {
            console.error('[sendMessage] Error:', error);
            addMessage('bot', `
                <div style="color: #e74c3c; padding: 1rem; background: #fdf2f2; border-radius: 10px; border-left: 4px solid #e74c3c;">
                    <strong>Ошибка</strong><br>
                    ${error.message || 'Не удалось получить ответ.'}
                </div>
            `);
        } finally {
            hideLoading();
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


