/* ========================================
   SMART STUDY PLANNER — Main JavaScript
   ======================================== */

// ========== CLOCK ==========
function updateClock() {
    const now = new Date();
    const time = now.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const date = now.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
    const el = document.getElementById('currentTime');
    if (el) el.textContent = `${date} · ${time}`;
}
setInterval(updateClock, 1000);
updateClock();

// ========== SIDEBAR ==========
const sidebar = document.getElementById('sidebar');
const menuToggle = document.getElementById('menuToggle');
const sidebarClose = document.getElementById('sidebarClose');

if (menuToggle) {
    menuToggle.addEventListener('click', () => {
        sidebar.classList.toggle('open');
    });
}

if (sidebarClose) {
    sidebarClose.addEventListener('click', () => {
        sidebar.classList.remove('open');
    });
}

// Close sidebar on outside click (mobile)
document.addEventListener('click', (e) => {
    if (window.innerWidth <= 768 && sidebar.classList.contains('open')) {
        if (!sidebar.contains(e.target) && !menuToggle.contains(e.target)) {
            sidebar.classList.remove('open');
        }
    }
});

// ========== MODAL SYSTEM ==========
function openModal(id) {
    const modal = document.getElementById(id);
    const overlay = document.getElementById('modalOverlay');
    if (modal) modal.classList.add('active');
    if (overlay) overlay.classList.add('active');
}

function closeModal(id) {
    const modal = document.getElementById(id);
    const overlay = document.getElementById('modalOverlay');
    if (modal) modal.classList.remove('active');
    if (overlay) overlay.classList.remove('active');
}

// Close modal on overlay click
document.addEventListener('click', (e) => {
    if (e.target.id === 'modalOverlay') {
        document.querySelectorAll('.modal.active').forEach(m => m.classList.remove('active'));
        e.target.classList.remove('active');
    }
});

// Close modal on Escape
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        document.querySelectorAll('.modal.active').forEach(m => m.classList.remove('active'));
        const overlay = document.getElementById('modalOverlay');
        if (overlay) overlay.classList.remove('active');
    }
});

// ========== TOAST NOTIFICATIONS ==========
function showToast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    if (!container) return;

    const icons = {
        success: 'fa-check-circle',
        info: 'fa-info-circle',
        error: 'fa-exclamation-circle',
        warning: 'fa-exclamation-triangle'
    };

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.innerHTML = `<i class="fas ${icons[type] || icons.info}"></i><span>${message}</span>`;
    container.appendChild(toast);

    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(100px)';
        toast.style.transition = 'all 0.3s ease';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

// ========== PAGE LOAD ANIMATION ==========
document.addEventListener('DOMContentLoaded', () => {
    // Stagger fade-in for stat cards
    document.querySelectorAll('.stat-card').forEach((card, i) => {
        card.style.opacity = '0';
        card.style.transform = 'translateY(20px)';
        setTimeout(() => {
            card.style.transition = 'all 0.5s cubic-bezier(0.4, 0, 0.2, 1)';
            card.style.opacity = '1';
            card.style.transform = 'translateY(0)';
        }, 100 + i * 100);
    });

    // Stagger fade-in for cards
    document.querySelectorAll('.card, .subject-card, .note-card, .suggestion-card').forEach((card, i) => {
        card.style.opacity = '0';
        card.style.transform = 'translateY(15px)';
        setTimeout(() => {
            card.style.transition = 'all 0.4s cubic-bezier(0.4, 0, 0.2, 1)';
            card.style.opacity = '1';
            card.style.transform = 'translateY(0)';
        }, 200 + i * 80);
    });
});

// ========== GAMIFICATION PROFILE ==========
window.updateProfile = async function () {
    try {
        const res = await fetch('/api/profile');
        if (!res.ok) return;
        const data = await res.json();

        const levelEl = document.getElementById('navLevel');
        const xpEl = document.getElementById('navXp');
        const fillEl = document.getElementById('navXpFill');

        if (levelEl) levelEl.textContent = 'Lvl ' + data.level;
        if (xpEl) xpEl.textContent = data.xp;
        if (fillEl) {
            const percentage = ((data.xp % 500) / 500) * 100;
            fillEl.style.width = percentage + '%';
        }
    } catch (err) {
        console.error('Failed to update profile', err);
    }
};

// Also globally handle showing earned XP
window.showXpToast = function (xp) {
    if (xp > 0) {
        showToast(`+${xp} XP Earned! 🌟`, 'success');
        updateProfile();
    }
};

// Initialize profile on load
document.addEventListener('DOMContentLoaded', () => {
    updateProfile();
});

// ========== AI CHATBOT ==========
document.addEventListener('DOMContentLoaded', () => {
    const toggleBtn = document.getElementById('chatbotToggle');
    const closeBtn = document.getElementById('chatbotClose');
    const windowEl = document.getElementById('chatbotWindow');
    const sendBtn = document.getElementById('chatSend');
    const inputEl = document.getElementById('chatInput');
    const messagesEl = document.getElementById('chatbotMessages');

    if (!toggleBtn || !windowEl) return;

    toggleBtn.addEventListener('click', () => {
        windowEl.classList.toggle('active');
        if (windowEl.classList.contains('active')) {
            inputEl.focus();
        }
    });

    closeBtn.addEventListener('click', () => {
        windowEl.classList.remove('active');
    });

    function addMessage(text, isUser = false) {
        const msgDiv = document.createElement('div');
        msgDiv.className = `chat-msg ${isUser ? 'user' : 'ai'}`;

        msgDiv.innerHTML = `
            ${!isUser ? '<div class="msg-avatar"><i class="fas fa-robot"></i></div>' : ''}
            <div class="msg-bubble">${text}</div>
        `;

        messagesEl.appendChild(msgDiv);
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    async function sendMessage() {
        const text = inputEl.value.trim();
        if (!text) return;

        // Add user message
        addMessage(text, true);
        inputEl.value = '';
        inputEl.disabled = true;
        sendBtn.disabled = true;

        // Add typing indicator
        const typingId = 'typing-' + Date.now();
        const typingDiv = document.createElement('div');
        typingDiv.className = 'chat-msg ai';
        typingDiv.id = typingId;
        typingDiv.innerHTML = `
            <div class="msg-avatar"><i class="fas fa-robot"></i></div>
            <div class="msg-bubble"><i class="fas fa-ellipsis-h fa-fade"></i> Thinking...</div>
        `;
        messagesEl.appendChild(typingDiv);
        messagesEl.scrollTop = messagesEl.scrollHeight;

        try {
            const res = await fetch('/api/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: text })
            });
            const data = await res.json();

            // Remove typing indicator
            document.getElementById(typingId).remove();

            // Add AI response
            addMessage(data.reply, false);

        } catch (err) {
            document.getElementById(typingId).remove();
            addMessage("Sorry, I'm having trouble connecting to the server.", false);
        } finally {
            inputEl.disabled = false;
            sendBtn.disabled = false;
            inputEl.focus();
        }
    }

    sendBtn.addEventListener('click', sendMessage);
    inputEl.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') sendMessage();
    });
});
