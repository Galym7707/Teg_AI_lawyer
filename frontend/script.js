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
    const API_BASE = (window.env && window.env.BACKEND_URL)
        ? window.env.BACKEND_URL + '/api'
        : 'https://tegailawyer-production.up.railway.app/api';

    // --- PRESET ANSWERS FOR DEMO ---
    const PRESET_ANSWERS = {
        "Как правильно уволиться с работы?": `<h3>Юридическая оценка</h3>
<p>Общий порядок увольнения по инициативе работника в Республике Казахстан предусматривает <strong>письменное уведомление работодателя</strong> и соблюдение срока предупреждения, который обычно составляет <strong>не менее 1 месяца</strong> (если иное не согласовано сторонами). Работодатель обязан произвести окончательный расчёт и выдать документы в установленный срок.</p>

<h3>Что делать пошагово</h3>
<ul>
  <li><strong>Подготовить заявление.</strong> Кратко: прошу уволить по собственному желанию с указанием даты увольнения.</li>
  <li><strong>Передать работодателю под отметку.</strong> Лично в канцелярию/HR и получить входящий номер или подпись о получении.</li>
  <li><strong>Отработать срок предупреждения.</strong> Возможна договорённость об увольнении раньше указанной даты (по соглашению сторон).</li>
  <li><strong>Получить расчёт и документы.</strong> Зарплата, компенсация за неиспользованный отпуск, иные выплаты; выдать/подтвердить выдачу трудовых документов.</li>
  <li><strong>При споре.</strong> Письменная претензия работодателю → инспекция труда → суд (при необходимости).</li>
</ul>

<h3>Мини-шаблон заявления</h3>
<pre class="code-block"><code>Руководителю ___________________________
от ____________________________________
должность ______________________________

ЗАЯВЛЕНИЕ
Прошу уволить меня по собственному желанию с «___»________20__ г.
С условиями окончательного расчёта ознакомлен(а).

Дата ____________    Подпись _____________ /ФИО/
</code></pre>

<h3>Какие документы иметь при себе</h3>
<ul>
  <li>Удостоверение личности.</li>
  <li>Трудовой договор/приказ о приёме (если есть копии).</li>
  <li>Справки по зарплате (при наличии), табель/график — для сверки расчётов.</li>
</ul>

<h3>Полезно знать</h3>
<ul>
  <li>Срок предупреждения может быть изменён <strong>по соглашению сторон</strong> (например, без отработки).</li>
  <li>Компенсацию за неиспользованный отпуск выплачивают при увольнении.</li>
</ul>`,

        "Какие права у арендатора жилья?": `<h3>Ключевые права арендатора</h3>
<ul>
  <li><strong>Письменный договор найма.</strong> Договор фиксирует цену, срок, права/обязанности, залог, порядок расторжения и возврата имущества.</li>
  <li><strong>Надлежащее состояние жилья.</strong> Жильё должно быть пригодно для проживания, исправные коммуникации и безопасность.</li>
  <li><strong>Конфиденциальность и спокойное владение.</strong> Собственник не вправе заходить без согласования, кроме аварийных случаев.</li>
  <li><strong>Прозрачные платежи.</strong> Арендная плата и коммунальные — по договору и/или показаниям. Изменение условий — только по договору или с уведомлением в предусмотренные сроки.</li>
  <li><strong>Залог (депозит).</strong> Возвращается при выезде при отсутствии ущерба/задолженности (с актом сверки и актом возврата).</li>
</ul>

<h3>Что проверить и как защититься</h3>
<ul>
  <li><strong>Акт приёма-передачи</strong> с перечислением мебели/техники, фотофиксация состояния.</li>
  <li><strong>Квитанции/переводы</strong> по оплатам — хранить всю историю платежей.</li>
  <li><strong>Претензия арендодателю</strong> при нарушениях (письменно, с сроком на устранение). При неисполнении — медиация/суд.</li>
</ul>

<h3>Мини-чек-лист перед подписанием</h3>
<ul>
  <li>Право собственности у арендодателя (свидетельство/выписка).</li>
  <li>Точный адрес, срок, стоимость, коммунальные (кто платит), размер и условия возврата депозита.</li>
  <li>Правила визитов собственника и досрочного расторжения (срок уведомления).</li>
</ul>`,

        "Как подать на алименты?": `<h3>Варианты взыскания</h3>
<ul>
  <li><strong>Нотариальное соглашение об уплате алиментов.</strong> Заключается между родителями, удостоверяется у нотариуса, имеет силу исполнительного документа.</li>
  <li><strong>Через суд.</strong> 
    <ul>
      <li><em>Судебный приказ</em> — быстрый порядок при бесспорных требованиях.</li>
      <li><em>Исковое производство</em> — если есть спор (о доле, сумме, дополнительных расходах и т.п.).</li>
    </ul>
  </li>
</ul>

<h3>Размер</h3>
<p>Чаще всего суд взыскивает долю от дохода плательщика: <strong>1/4 — на одного ребёнка, 1/3 — на двоих, 1/2 — на трёх и более</strong>. Возможна твёрдая денежная сумма, если доход нерегулярен.</p>

<h3>Документы</h3>
<ul>
  <li>Удостоверение личности заявителя.</li>
  <li>Свидетельство о рождении ребёнка(детей).</li>
  <li>Справки о доходах (при наличии), расходы на ребёнка (чеки/квитанции) — по ситуации.</li>
  <li>Данные о месте работы/доходах ответчика (если известны).</li>
</ul>

<h3>Порядок действий</h3>
<ul>
  <li>Попробовать <strong>нотариальное соглашение</strong> (быстро и без суда).</li>
  <li>Либо подать в суд заявление (на судебный приказ или иск) по месту жительства ответчика/истца (в установленных случаях).</li>
  <li>Получить исполнительный документ и обратиться к <strong>частному судебному исполнителю</strong> для принудительного взыскания.</li>
</ul>

<h3>Важно</h3>
<ul>
  <li>Алименты назначаются <strong>с даты обращения</strong> (суд/нотариус), поэтому не затягивайте.</li>
  <li>При изменении обстоятельств (доход, состояние здоровья) возможно изменение размера алиментов через суд.</li>
</ul>`,

        "Что делать при ДТП?": `<h3>Безопасность на первом месте</h3>
<ul>
  <li>Остановитесь, включите аварийку, выставьте знак аварийной остановки.</li>
  <li>Есть пострадавшие или угрозы? Звоните <strong>112</strong>, для полиции — <strong>102</strong>.</li>
</ul>

<h3>Фиксация обстоятельств</h3>
<ul>
  <li>Фото/видео положения авто, следов, повреждений, дорожных знаков, камеры, свидетелей.</li>
  <li>Обмен данными: ФИО, ИИН, контакты, полис ОСАГО, номер ТС, водительское удостоверение.</li>
</ul>

<h3>Оформление</h3>
<ul>
  <li>При разногласиях, травмах или значительном ущербе — <strong>ожидайте полицию</strong>.</li>
  <li>Если без пострадавших и спорных вопросов, действуйте по инструкции страховщика (упрощённое оформление, если применимо).</li>
</ul>

<h3>Страховая</h3>
<ul>
  <li>Сообщите в свою страховую <strong>в установленные договором сроки</strong> (как правило, незамедлительно).</li>
  <li>Передайте пакет документов, пройдите осмотр ТС.</li>
</ul>

<h3>Полезно</h3>
<ul>
  <li>Не перемещайте авто до фиксации, если это не мешает движению (или отметьте положение, затем освободите полосу).</li>
  <li>Не подписывайте документы, смысл которых не понимаете — просите разъяснение.</li>
</ul>`
    };

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

    // Проверка готовности сервера
    async function checkServerReady() {
        try {
            const res = await apiFetch("/health");
            return res.ok && (await res.json()).index_ready;
        } catch {
            return false;
        }
    }

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

        // 1) Пресетный ответ для демо-вопросов
        const presetHtml = PRESET_ANSWERS[messageText];
        if (presetHtml) {
            // показать сообщение пользователя как обычно
            addMessage('user', messageText);
            
            // показать «думает…»
            const aiMessageElement = addMessage('bot', 'ИИ-юрист анализирует ваш запрос…');
            
            // имитация 2 секунд «обдумывания»
            await new Promise(r => setTimeout(r, 2000));
            updateBotMessage(aiMessageElement, presetHtml);
            
            // Clear input and reset height
            if (!message) {
                userInput.value = '';
                autoResize(userInput);
            }
            return; // важный ранний выход — не зовём бэкенд
        }

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
            // Проверка готовности сервера
            if (!(await checkServerReady())) {
                addMessage('bot', `
                    <div style="color: #e67e22; padding: 1rem; background: #fef9e7; border-radius: 10px;">
                        Сервер готовится к работе... Пожалуйста, попробуйте через 10-15 секунд.
                    </div>
                `);
                hideLoading();
                return;
            }

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


