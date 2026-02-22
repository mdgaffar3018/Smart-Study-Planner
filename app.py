import sqlite3
import os
import json
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, g, redirect, url_for, flash, session
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from flask import Flask, render_template, request, jsonify, g, redirect, url_for, flash, session
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv

# Load environment variables from .env file if present
load_dotenv()

# --- Google Gemini AI ---
import urllib.request
import urllib.error

app = Flask(__name__)
# Try to securely load the secret key, otherwise fallback for local dev
app.secret_key = os.environ.get('SECRET_KEY', 'dev_super_secret_key_12345')

# --- OAuth Setup ---
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=os.environ.get('GOOGLE_CLIENT_ID'),
    client_secret=os.environ.get('GOOGLE_CLIENT_SECRET'),
    access_token_url='https://accounts.google.com/o/oauth2/token',
    access_token_params=None,
    authorize_url='https://accounts.google.com/o/oauth2/auth',
    authorize_params=None,
    api_base_url='https://www.googleapis.com/oauth2/v1/',
    client_kwargs={'scope': 'openid email profile'},
)

# Use /tmp for database on Vercel deployment, otherwise local directory
if os.environ.get('VERCEL') == '1' or os.environ.get('VERCEL_ENV'):
    DATABASE = '/tmp/study_planner.db'
else:
    DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'study_planner.db')

# --- Groq AI Configuration (Open Source LLaMA) ---
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')
ai_client_active = True if GROQ_API_KEY else False

def call_groq_rest(prompt, model_name='llama3-8b-8192'):
    url = "https://api.groq.com/openai/v1/chat/completions"
    data = json.dumps({
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7
    }).encode('utf-8')
    
    req = urllib.request.Request(url, data=data, headers={
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {GROQ_API_KEY}',
        'User-Agent': 'SmartStudyPlanner/1.0'
    })
    
    with urllib.request.urlopen(req) as response:
        result = json.loads(response.read().decode())
        return result['choices'][0]['message']['content'].strip()


# --- Authentication Setup ---
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message_category = 'info'

class User(UserMixin):
    def __init__(self, id, email, name, password_hash, google_id):
        self.id = id
        self.email = email
        self.name = name
        self.password_hash = password_hash
        self.google_id = google_id

@login_manager.user_loader
def load_user(user_id):
    db = get_db()
    user_data = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if user_data:
        return User(user_data['id'], user_data['email'], user_data['name'], user_data['password_hash'], user_data['google_id'])
    return None

# ===================== DATABASE =====================

def get_db():
    if 'db' not in g:
        db_exists = os.path.exists(DATABASE)
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        
        # Avoid WAL mode on Vercel as it can cause issues in /tmp
        if not (os.environ.get('VERCEL') == '1' or os.environ.get('VERCEL_ENV')):
            g.db.execute("PRAGMA journal_mode=WAL")
            
        g.db.execute("PRAGMA foreign_keys=ON")
        
        # If the DB was just created (common on Vercel cold starts), initialize it
        if not db_exists:
            init_db_with_connection(g.db)
            
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db:
        db.close()


def init_db():
    db = sqlite3.connect(DATABASE)
    init_db_with_connection(db)
    db.close()

def init_db_with_connection(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT,
            google_id TEXT UNIQUE,
            name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            color TEXT DEFAULT '#6C63FF',
            icon TEXT DEFAULT 'fa-book',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            subject_id INTEGER,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            priority TEXT DEFAULT 'medium',
            deadline TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS study_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            subject_id INTEGER,
            duration_minutes INTEGER NOT NULL,
            session_type TEXT DEFAULT 'manual',
            notes TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            target_hours REAL DEFAULT 10,
            current_hours REAL DEFAULT 0,
            deadline TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            subject_id INTEGER,
            title TEXT NOT NULL,
            content TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS planner_blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            subject_id INTEGER,
            day_of_week INTEGER NOT NULL,
            start_hour INTEGER NOT NULL,
            end_hour INTEGER NOT NULL,
            title TEXT DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS user_profile (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE NOT NULL,
            xp INTEGER DEFAULT 0,
            level INTEGER DEFAULT 1,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
    ''')


# ===================== AI SUGGESTIONS =====================

def get_study_context(db):
    """Gathers user context for AI prompt."""
    if not current_user.is_authenticated:
        return None
        
    profile = db.execute('SELECT * FROM user_profile WHERE user_id = ?', (current_user.id,)).fetchone()
    if not profile:
        profile = {'xp': 0, 'level': 1}
        
    subjects = db.execute('SELECT id, name FROM subjects WHERE user_id = ?', (current_user.id,)).fetchall()
    pending_tasks = db.execute('''
        SELECT t.title, s.name as subject, t.deadline, t.priority 
        FROM tasks t 
        LEFT JOIN subjects s ON t.subject_id = s.id 
        WHERE t.user_id = ? AND t.status = "pending" 
        ORDER BY t.priority DESC, t.deadline ASC LIMIT 5
    ''', (current_user.id,)).fetchall()
    goals = db.execute('SELECT * FROM goals WHERE user_id = ? AND status = "active"', (current_user.id,)).fetchall()

    # Calculate study hours per subject
    hours_per_subject = db.execute('''
        SELECT s.name, COALESCE(SUM(ss.duration_minutes), 0) / 60.0 as hours
        FROM subjects s
        LEFT JOIN study_sessions ss ON s.id = ss.subject_id
        WHERE s.user_id = ?
        GROUP BY s.id
    ''', (current_user.id,)).fetchall()

    # Recent 7-day totals
    week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    weekly_hours_row = db.execute('''
        SELECT COALESCE(SUM(duration_minutes), 0) / 60.0 as hours
        FROM study_sessions WHERE user_id = ? AND created_at >= ?
    ''', (current_user.id, week_ago,)).fetchone()
    weekly_hours = weekly_hours_row['hours'] if weekly_hours_row else 0

    total_sessions_row = db.execute('SELECT COUNT(*) as c FROM study_sessions WHERE user_id = ?', (current_user.id,)).fetchone()
    total_sessions = total_sessions_row['c'] if total_sessions_row else 0

    context = {
        'subjects': [dict(s) for s in subjects],
        'pending_tasks': [dict(t) for t in pending_tasks],
        'completed_tasks_count': db.execute('SELECT COUNT(*) as c FROM tasks WHERE user_id = ? AND status = "completed"', (current_user.id,)).fetchone()['c'],
        'hours_per_subject': {row['name']: round(row['hours'], 1) for row in hours_per_subject},
        'weekly_study_hours': round(weekly_hours, 1),
        'active_goals': [dict(g) for g in goals],
        'recent_sessions_count': db.execute('SELECT COUNT(*) as c FROM study_sessions WHERE user_id = ? ORDER BY created_at DESC LIMIT 20', (current_user.id,)).fetchone()['c'],
        'total_sessions': total_sessions,
        'profile': dict(profile)
    }
    return context


def get_ai_suggestions(db):
    """Factory function to get suggestions either from AI or fallback rules."""
    context = get_study_context(db)
    if not context:
        return [] # No context if user not authenticated
    
    suggestions = []
    ai_powered = False

    if ai_client_active:
        try:
            suggestions = get_groq_suggestions(context)
            if suggestions:
                ai_powered = True
        except Exception:
            pass # Fallback to smart rules if AI fails
    
    # Ensure we only return 4 valid suggestions
    valid_suggestions = [s for s in suggestions if isinstance(s, dict) and all(k in s for k in ['title', 'description', 'type', 'priority'])][:4]
    
    # If we somehow got fewer than 4 or invalid ones, pad with fallbacks
    if len(valid_suggestions) < 4:
         fallback_sugs = get_smart_fallback_suggestions(context)
         valid_suggestions.extend(fallback_sugs[:4 - len(valid_suggestions)])
         
    # Mark them with the type for the frontend
    if ai_powered:
        for s in valid_suggestions:
            s['source'] = 'ai'
    else:
        for s in valid_suggestions:
            s['source'] = 'smart_rules'
            
    return valid_suggestions

def get_groq_suggestions(context):
    """Use Groq LLaMA to generate study suggestions."""
    prompt = f"""You are a smart study planner AI assistant. Based on the following student data, provide exactly 4 actionable study suggestions. Each suggestion should have a title (max 8 words), a description (max 25 words), a type (one of: schedule, focus, break, goal, revision, balance), and a priority (high, medium, low).

Student Data:
- Subjects: {json.dumps(context['subjects'], default=str)}
- Pending Tasks: {json.dumps(context['pending_tasks'][:5], default=str)}
- Study Hours by Subject: {json.dumps(context['hours_per_subject'])}
- Weekly Study Hours: {context['weekly_study_hours']}
- Active Goals: {json.dumps(context['active_goals'], default=str)}
- Total Study Sessions: {context['total_sessions']}

Current date/time: {datetime.now().strftime('%Y-%m-%d %H:%M')}

Respond ONLY with a valid JSON array of 4 objects, each with keys: "title", "description", "type", "priority". No markdown, no extra text."""

    try:
        text = call_groq_rest(prompt, model_name='llama3-8b-8192')
        # Clean potential markdown wrapping
        if text.startswith('```'):
            text = text.split('\n', 1)[1]
            text = text.rsplit('```', 1)[0]
        suggestions = json.loads(text)
        if isinstance(suggestions, list) and len(suggestions) > 0:
            return suggestions[:4]
    except Exception:
        pass

    return get_smart_fallback_suggestions(context)


def get_smart_fallback_suggestions(context):
    """Rule-based smart suggestions when AI is not available."""
    suggestions = []
    now = datetime.now()
    hour = now.hour

    # 1. Check for overdue / urgent tasks
    urgent_tasks = []
    for t in context['pending_tasks']:
        if t.get('deadline'):
            try:
                dl = datetime.strptime(t['deadline'], '%Y-%m-%d')
                days_left = (dl - now).days
                if days_left <= 2:
                    urgent_tasks.append((t, days_left))
            except ValueError:
                pass

    if urgent_tasks:
        task, days = urgent_tasks[0]
        subj = task.get('subject_name', 'Unknown')
        if days < 0:
            suggestions.append({
                'title': f'Overdue: {task["title"][:20]}',
                'description': f'This task for {subj} is past its deadline. Complete it ASAP!',
                'type': 'focus',
                'priority': 'high'
            })
        else:
            suggestions.append({
                'title': f'Urgent deadline approaching',
                'description': f'"{task["title"][:15]}" is due in {days} day(s). Prioritize it now.',
                'type': 'focus',
                'priority': 'high'
            })

    # 2. Study balance check
    hours = context['hours_per_subject']
    if hours:
        max_subj = max(hours, key=hours.get)
        min_subj = min(hours, key=hours.get)
        if hours[max_subj] > 0 and hours[min_subj] < hours[max_subj] * 0.3:
            suggestions.append({
                'title': f'Balance your study time',
                'description': f'You\'ve studied {min_subj} much less than {max_subj}. Dedicate more time to it.',
                'type': 'balance',
                'priority': 'medium'
            })

    # 3. Time-based suggestion
    if hour < 12:
        suggestions.append({
            'title': 'Morning focus session',
            'description': 'Mornings are great for complex topics. Start a deep study session now!',
            'type': 'schedule',
            'priority': 'medium'
        })
    elif hour < 17:
        suggestions.append({
            'title': 'Afternoon revision block',
            'description': 'Review what you studied this morning for better retention.',
            'type': 'revision',
            'priority': 'medium'
        })
    else:
        suggestions.append({
            'title': 'Evening light review',
            'description': 'Do a light recap or flashcard session before winding down.',
            'type': 'revision',
            'priority': 'low'
        })

    # 4. Weekly hours check
    if context['weekly_study_hours'] < 5:
        suggestions.append({
            'title': 'Boost your weekly hours',
            'description': f'Only {context["weekly_study_hours"]}h this week. Aim for at least 10 hours.',
            'type': 'goal',
            'priority': 'high'
        })
    elif context['weekly_study_hours'] > 30:
        suggestions.append({
            'title': 'Remember to take breaks',
            'description': 'You\'ve studied a lot this week. Rest is important for memory!',
            'type': 'break',
            'priority': 'medium'
        })
    else:
        suggestions.append({
            'title': 'Great study momentum!',
            'description': f'{context["weekly_study_hours"]}h this week. Keep up the consistent effort!',
            'type': 'goal',
            'priority': 'low'
        })

    # 5. Goals progress
    for goal in context['active_goals']:
        pct = (goal['current_hours'] / goal['target_hours'] * 100) if goal['target_hours'] > 0 else 0
        if pct < 30 and goal.get('deadline'):
            suggestions.append({
                'title': f'Goal needs attention',
                'description': f'"{goal["title"][:15]}" is only {pct:.0f}% complete. Step it up!',
                'type': 'goal',
                'priority': 'high'
            })
            break

    # 6. No subjects yet
    if not context['subjects']:
        suggestions.append({
            'title': 'Add your subjects',
            'description': 'Start by adding the subjects you\'re studying to organize your plan.',
            'type': 'schedule',
            'priority': 'high'
        })

    # 7. Pomodoro suggestion
    if context['total_sessions'] < 3:
        suggestions.append({
            'title': 'Try the Pomodoro Timer',
            'description': 'Use 25-min focused sessions with 5-min breaks for better productivity.',
            'type': 'focus',
            'priority': 'medium'
        })

    return suggestions[:4]


# ===================== PAGE ROUTES =====================

@app.route('/')
@login_required
def dashboard():
    return render_template('dashboard.html', active='dashboard')


@app.route('/subjects')
@login_required
def subjects_page():
    return render_template('subjects.html', active='subjects')


@app.route('/tasks')
@login_required
def tasks_page():
    return render_template('tasks.html', active='tasks')


@app.route('/timer')
@login_required
def timer_page():
    return render_template('timer.html', active='timer')


@app.route('/analytics')
@login_required
def analytics_page():
    return render_template('analytics.html', active='analytics')


@app.route('/planner')
@login_required
def planner_page():
    return render_template('planner.html', active='planner')


@app.route('/notes')
@login_required
def notes_page():
    return render_template('notes.html', active='notes')


# ===================== AUTHENTICATION ROUTES =====================

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
        
    if request.method == 'POST':
        email = request.form.get('email')
        name = request.form.get('name')
        password = request.form.get('password')
        
        db = get_db()
        existing_user = db.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()
        
        if existing_user:
            flash('Email address already exists. Please log in.', 'error')
            return redirect(url_for('register'))
            
        password_hash = generate_password_hash(password)
        cursor = db.execute('INSERT INTO users (email, name, password_hash) VALUES (?, ?, ?)', (email, name, password_hash))
        
        # Create initial profile
        user_id = cursor.lastrowid
        db.execute('INSERT INTO user_profile (user_id, xp, level) VALUES (?, 0, 1)', (user_id,))
        db.commit()
        
        # Auto-login after register
        new_user = load_user(user_id)
        login_user(new_user)
        return redirect(url_for('dashboard'))
        
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
        
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        db = get_db()
        user_data = db.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
        
        if user_data and user_data['password_hash'] and check_password_hash(user_data['password_hash'], password):
            user = load_user(user_data['id'])
            login_user(user)
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid email or password.', 'error')
            
    return render_template('login.html')

@app.route('/login/google')
def login_google():
    if not os.environ.get('GOOGLE_CLIENT_ID'):
        flash('Google Client ID not configured on this server.', 'error')
        return redirect(url_for('login'))
    redirect_uri = url_for('auth_callback', _external=True)
    return google.authorize_redirect(redirect_uri)

@app.route('/auth/callback')
def auth_callback():
    token = google.authorize_access_token()
    user_info = google.parse_id_token(token, nonce=None)
    
    if not user_info:
        flash('Failed to authenticate with Google.', 'error')
        return redirect(url_for('login'))
        
    email = user_info.get('email')
    name = user_info.get('name')
    google_id = user_info.get('sub')
    
    db = get_db()
    
    # Check if user already exists
    user_data = db.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()
    
    if user_data:
        # Existing user logs in (maybe link google account if not linked)
        db.execute('UPDATE users SET google_id = ?, name = COALESCE(name, ?) WHERE email = ?', (google_id, name, email))
        db.commit()
        user_id = user_data['id']
    else:
        # Create new user via Google
        cursor = db.execute('INSERT INTO users (email, name, google_id) VALUES (?, ?, ?)', (email, name, google_id))
        user_id = cursor.lastrowid
        db.execute('INSERT INTO user_profile (user_id, xp, level) VALUES (?, 0, 1)', (user_id,))
        db.commit()
        
    user = load_user(user_id)
    login_user(user)
    return redirect(url_for('dashboard'))

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# ===================== ROUTES =====================

# --- Gamification / Profile ---
def calculate_level(xp):
    return (xp // 500) + 1 # 500 XP per level

def award_xp(db, xp_amount):
    if not current_user.is_authenticated:
        return
        
    current = db.execute('SELECT xp, level FROM user_profile WHERE user_id = ?', (current_user.id,)).fetchone()
    if not current:
        db.execute('INSERT INTO user_profile (user_id, xp, level) VALUES (?, ?, 1)', (current_user.id, xp_amount))
    else:
        new_xp = current['xp'] + xp_amount
        new_level = calculate_level(new_xp)
        db.execute('UPDATE user_profile SET xp=?, level=? WHERE user_id = ?', (new_xp, new_level, current_user.id))
    db.commit()

@app.route('/api/profile', methods=['GET'])
@login_required
def api_profile():
    db = get_db()
    profile = db.execute('SELECT xp, level FROM user_profile WHERE user_id = ?', (current_user.id,)).fetchone()
    if not profile:
        profile = {'xp': 0, 'level': 1}
    return jsonify(dict(profile))

# --- Stats ---
@app.route('/api/stats')
@login_required
def api_stats():
    db = get_db()
    total_hours = db.execute('SELECT COALESCE(SUM(duration_minutes), 0) / 60.0 as h FROM study_sessions WHERE user_id = ?', (current_user.id,)).fetchone()['h']
    tasks_done = db.execute('SELECT COUNT(*) as c FROM tasks WHERE user_id = ? AND status = "completed"', (current_user.id,)).fetchone()['c']
    tasks_total = db.execute('SELECT COUNT(*) as c FROM tasks WHERE user_id = ?', (current_user.id,)).fetchone()['c']
    subjects_count = db.execute('SELECT COUNT(*) as c FROM subjects WHERE user_id = ?', (current_user.id,)).fetchone()['c']
    sessions_count = db.execute('SELECT COUNT(*) as c FROM study_sessions WHERE user_id = ?', (current_user.id,)).fetchone()['c']

    # Weekly data (last 7 days)
    weekly = []
    for i in range(6, -1, -1):
        day = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
        day_label = (datetime.now() - timedelta(days=i)).strftime('%a')
        hours = db.execute(
            'SELECT COALESCE(SUM(duration_minutes), 0) / 60.0 as h FROM study_sessions WHERE user_id = ? AND DATE(created_at) = ?',
            (current_user.id, day,)
        ).fetchone()['h']
        weekly.append({'day': day_label, 'hours': round(hours, 1)})

    # Streak calculation
    streak = 0
    for i in range(0, 60):
        day = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
        count = db.execute('SELECT COUNT(*) as c FROM study_sessions WHERE user_id = ? AND DATE(created_at) = ?', (current_user.id, day,)).fetchone()['c']
        if count > 0:
            streak += 1
        else:
            if i > 0:
                break

    # Upcoming deadlines
    upcoming = db.execute('''
        SELECT t.*, s.name as subject_name, s.color as subject_color
        FROM tasks t LEFT JOIN subjects s ON t.subject_id = s.id
        WHERE t.user_id = ? AND t.status = 'pending' AND t.deadline IS NOT NULL
        ORDER BY t.deadline LIMIT 5
    ''', (current_user.id,)).fetchall()

    # Recent sessions
    recent_sessions = db.execute('''
        SELECT ss.*, s.name as subject_name, s.color as subject_color
        FROM study_sessions ss LEFT JOIN subjects s ON ss.subject_id = s.id
        WHERE ss.user_id = ?
        ORDER BY ss.created_at DESC LIMIT 5
    ''', (current_user.id,)).fetchall()

    return jsonify({
        'total_hours': round(total_hours, 1),
        'tasks_done': tasks_done,
        'tasks_total': tasks_total,
        'subjects_count': subjects_count,
        'sessions_count': sessions_count,
        'streak': streak,
        'weekly': weekly,
        'upcoming': [dict(u) for u in upcoming],
        'recent_sessions': [dict(s) for s in recent_sessions]
    })


# --- AI Suggestions ---
@app.route('/api/suggestions')
@login_required
def api_suggestions():
    db = get_db()
    suggestions = get_ai_suggestions(db)
    ai_powered = bool(ai_client_active)
    return jsonify({'suggestions': suggestions, 'ai_powered': ai_powered})


# --- AI Chatbot ---
@app.route('/api/chat', methods=['POST'])
@login_required
def api_chat():
    data = request.json
    user_message = data.get('message', '')
    
    if not ai_client_active:
        return jsonify({'reply': 'Sorry, the AI Buddy is currently offline. Please set the GROQ_API_KEY environment variable.'})
        
    db = get_db()
    context = get_study_context(db)
    
    prompt = f"""You are 'StudyAI', a friendly, encouraging, and highly intelligent study buddy chatbot.
The user just said: "{user_message}"

Here is their current study context:
- Level: {context['profile']['level']} (XP: {context['profile']['xp']})
- Subjects: {json.dumps(context['subjects'], default=str)}
- Pending Tasks: {len(context['pending_tasks'])} remaining
- Top Pending Tasks: {json.dumps(context['pending_tasks'][:3], default=str)}
- Weekly Study Hours: {context['weekly_study_hours']}

Respond naturally, concisely, and helpfully. Keep it under 3-4 sentences. Use emojis if appropriate. Acknowledge their tasks or stats if it makes sense contextually. Do not use markdown outside of bolding text. Do not return JSON."""

    try:
        text = call_groq_rest(prompt, model_name='llama-3.3-70b-versatile')
        return jsonify({'reply': text})
    except urllib.error.HTTPError as e:
        return jsonify({'reply': f"[Groq Error: HTTP {e.code} - {e.read().decode()}]"})
    except Exception as e:
        return jsonify({'reply': f"[Groq Error: {str(e)}]"})

# --- Subjects ---
@app.route('/api/subjects', methods=['GET'])
@login_required
def api_get_subjects():
    db = get_db()
    subjects = db.execute('''
        SELECT s.*, 
            COALESCE(SUM(ss.duration_minutes), 0) / 60.0 as total_hours,
            COUNT(DISTINCT t.id) as task_count
        FROM subjects s
        LEFT JOIN study_sessions ss ON s.id = ss.subject_id
        LEFT JOIN tasks t ON s.id = t.subject_id AND t.status = 'pending'
        WHERE s.user_id = ?
        GROUP BY s.id
        ORDER BY s.name
    ''', (current_user.id,)).fetchall()
    return jsonify([dict(s) for s in subjects])


@app.route('/api/subjects', methods=['POST'])
@login_required
def api_add_subject():
    data = request.json
    db = get_db()
    cursor = db.execute(
        'INSERT INTO subjects (user_id, name, color, icon) VALUES (?, ?, ?, ?)',
        (current_user.id, data.get('name'), data.get('color', '#6C63FF'), data.get('icon', 'fa-book'))
    )
    db.commit()
    award_xp(db, 10) # 10 xp for creating subject
    return jsonify({'success': True, 'id': cursor.lastrowid})


@app.route('/api/subjects/<int:id>', methods=['PUT'])
@login_required
def api_update_subject(id):
    db = get_db()
    data = request.json
    db.execute('UPDATE subjects SET name=?, color=?, icon=? WHERE id=? AND user_id = ?',
               (data['name'], data.get('color', '#6C63FF'), data.get('icon', 'fa-book'), id, current_user.id))
    db.commit()
    return jsonify({'success': True})


@app.route('/api/subjects/<int:id>', methods=['DELETE'])
@login_required
def api_delete_subject(id):
    db = get_db()
    db.execute('DELETE FROM subjects WHERE id = ? AND user_id = ?', (id, current_user.id))
    db.commit()
    return jsonify({'success': True})


# --- Tasks ---
@app.route('/api/tasks', methods=['GET'])
@login_required
def api_get_tasks():
    db = get_db()
    tasks = db.execute('''
        SELECT t.*, s.name as subject_name, s.color as subject_color 
        FROM tasks t 
        LEFT JOIN subjects s ON t.subject_id = s.id
        WHERE t.user_id = ?
        ORDER BY 
            CASE status WHEN 'pending' THEN 0 ELSE 1 END,
            CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
            deadline ASC
    ''', (current_user.id,)).fetchall()
    return jsonify([dict(t) for t in tasks])


@app.route('/api/tasks', methods=['POST'])
@login_required
def api_add_task():
    data = request.json
    db = get_db()
    cursor = db.execute(
        'INSERT INTO tasks (user_id, subject_id, title, description, priority, deadline) VALUES (?, ?, ?, ?, ?, ?)',
        (current_user.id, data.get('subject_id'), data['title'], data.get('description', ''), 
         data.get('priority', 'medium'), data.get('deadline'))
    )
    db.commit()
    award_xp(db, 5) # 5 xp for creating task
    return jsonify({'success': True, 'id': cursor.lastrowid})


@app.route('/api/tasks/<int:id>', methods=['PUT'])
@login_required
def api_update_task(id):
    db = get_db()
    data = request.json
    db.execute('UPDATE tasks SET subject_id=?, title=?, description=?, priority=?, deadline=?, status=? WHERE id=? AND user_id = ?',
               (data.get('subject_id'), data['title'], data.get('description', ''),
                data.get('priority', 'medium'), data.get('deadline'), data.get('status', 'pending'), id, current_user.id))
    db.commit()
    return jsonify({'success': True})


@app.route('/api/tasks/<int:id>/toggle', methods=['POST'])
@login_required
def api_toggle_task(id):
    db = get_db()
    task = db.execute('SELECT status FROM tasks WHERE id=? AND user_id = ?', (id, current_user.id)).fetchone()
    earned_xp = 0
    if task:
        new_status = 'completed' if task['status'] != 'completed' else 'pending'
        db.execute('UPDATE tasks SET status=? WHERE id=? AND user_id = ?', (new_status, id, current_user.id))
        
        # Award XP for completing a task
        if new_status == 'completed':
            award_xp(db, 50)
            earned_xp = 50
            
        db.commit()
    return jsonify({'success': True, 'earned_xp': earned_xp})

@app.route('/api/tasks/<int:id>/status', methods=['PUT'])
@login_required
def api_update_task_status(id):
    status = request.json.get('status')
    db = get_db()
    
    # check if task was pending and is now completed
    prev = db.execute('SELECT status FROM tasks WHERE id = ? AND user_id = ?', (id, current_user.id)).fetchone()
    
    db.execute('UPDATE tasks SET status = ? WHERE id = ? AND user_id = ?', (status, id, current_user.id))
    db.commit()
    
    xp_awarded = 0
    if prev and prev['status'] != 'completed' and status == 'completed':
        xp_awarded = 25
        award_xp(db, xp_awarded)
        
    return jsonify({'success': True, 'xp_awarded': xp_awarded})


@app.route('/api/tasks/<int:id>', methods=['DELETE'])
@login_required
def api_delete_task(id):
    db = get_db()
    db.execute('DELETE FROM tasks WHERE id = ? AND user_id = ?', (id, current_user.id))
    db.commit()
    return jsonify({'success': True})


# --- Study Sessions ---
@app.route('/api/sessions', methods=['GET'])
@login_required
def api_get_sessions():
    db = get_db()
    sessions = db.execute('''
        SELECT ss.*, s.name as subject_name, s.color as subject_color
        FROM study_sessions ss LEFT JOIN subjects s ON ss.subject_id = s.id
        WHERE ss.user_id = ?
        ORDER BY ss.created_at DESC LIMIT 50
    ''', (current_user.id,)).fetchall()
    return jsonify([dict(s) for s in sessions])


@app.route('/api/sessions', methods=['POST'])
@login_required
def api_add_session():
    data = request.json
    db = get_db()
    duration = data['duration_minutes']
    
    # Calculate XP (roughly 2 XP per minute of study)
    xp_awarded = duration * 2
    
    cursor = db.execute(
        'INSERT INTO study_sessions (user_id, subject_id, duration_minutes, session_type, notes) VALUES (?, ?, ?, ?, ?)',
        (current_user.id, data.get('subject_id'), duration, 
         data.get('session_type', 'manual'), data.get('notes', ''))
    )
    db.commit()
    award_xp(db, xp_awarded)
    
    return jsonify({'success': True, 'id': cursor.lastrowid, 'xp_awarded': xp_awarded})


@app.route('/api/sessions/<int:id>', methods=['DELETE'])
@login_required
def api_delete_session(id):
    db = get_db()
    db.execute('DELETE FROM study_sessions WHERE id=? AND user_id = ?', (id, current_user.id))
    db.commit()
    return jsonify({'success': True})


# --- Goals ---
@app.route('/api/goals', methods=['GET'])
@login_required
def api_get_goals():
    db = get_db()
    goals = db.execute('SELECT * FROM goals WHERE user_id = ? ORDER BY status, deadline', (current_user.id,)).fetchall()
    return jsonify([dict(g) for g in goals])


@app.route('/api/goals', methods=['POST'])
@login_required
def api_add_goal():
    db = get_db()
    data = request.json
    db.execute('INSERT INTO goals (user_id, title, target_hours, deadline) VALUES (?,?,?,?)',
               (current_user.id, data['title'], data.get('target_hours', 10), data.get('deadline')))
    db.commit()
    return jsonify({'success': True})


@app.route('/api/goals/<int:id>', methods=['PUT'])
@login_required
def api_update_goal(id):
    db = get_db()
    data = request.json
    db.execute('UPDATE goals SET title=?, target_hours=?, current_hours=?, deadline=?, status=? WHERE id=? AND user_id = ?',
               (data['title'], data.get('target_hours', 10), data.get('current_hours', 0),
                data.get('deadline'), data.get('status', 'active'), id, current_user.id))
    db.commit()
    return jsonify({'success': True})


@app.route('/api/goals/<int:id>', methods=['DELETE'])
@login_required
def api_delete_goal(id):
    db = get_db()
    db.execute('DELETE FROM goals WHERE id=? AND user_id = ?', (id, current_user.id))
    db.commit()
    return jsonify({'success': True})


# --- Notes ---
@app.route('/api/notes', methods=['GET'])
@login_required
def api_get_notes():
    subject_id = request.args.get('subject_id')
    db = get_db()
    
    if subject_id:
        notes = db.execute('''
            SELECT n.*, s.name as subject_name, s.color as subject_color 
            FROM notes n 
            LEFT JOIN subjects s ON n.subject_id = s.id 
            WHERE n.subject_id = ? AND n.user_id = ? ORDER BY n.updated_at DESC
        ''', (subject_id, current_user.id)).fetchall()
    else:
        notes = db.execute('''
            SELECT n.*, s.name as subject_name, s.color as subject_color 
            FROM notes n 
            LEFT JOIN subjects s ON n.subject_id = s.id
            WHERE n.user_id = ?
            ORDER BY n.updated_at DESC
        ''', (current_user.id,)).fetchall()
        
    return jsonify([dict(n) for n in notes])


@app.route('/api/notes', methods=['POST'])
@login_required
def api_save_note():
    data = request.json
    db = get_db()
    
    note_id = data.get('id')
    if note_id:
        db.execute(
            'UPDATE notes SET subject_id=?, title=?, content=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?',
            (data.get('subject_id'), data.get('title'), data.get('content', ''), note_id, current_user.id)
        )
        award_xp(db, 5) # update note
    else:
        cursor = db.execute(
            'INSERT INTO notes (user_id, subject_id, title, content) VALUES (?, ?, ?, ?)',
            (current_user.id, data.get('subject_id'), data.get('title'), data.get('content', ''))
        )
        note_id = cursor.lastrowid
        award_xp(db, 15) # new note
        
    db.commit()
    return jsonify({'success': True, 'id': note_id})


@app.route('/api/notes/<int:id>', methods=['DELETE'])
@login_required
def api_delete_note(id):
    db = get_db()
    db.execute('DELETE FROM notes WHERE id = ? AND user_id = ?', (id, current_user.id))
    db.commit()
    return jsonify({'success': True})


# --- Planner Blocks ---
@app.route('/api/planner', methods=['GET'])
@login_required
def api_get_planner():
    db = get_db()
    blocks = db.execute('''
        SELECT p.*, s.name as subject_name, s.color as subject_color 
        FROM planner_blocks p 
        LEFT JOIN subjects s ON p.subject_id = s.id
        WHERE p.user_id = ?
        ORDER BY p.day_of_week, p.start_hour
    ''', (current_user.id,)).fetchall()
    return jsonify([dict(b) for b in blocks])


@app.route('/api/planner', methods=['POST'])
@login_required
def api_add_planner_block():
    data = request.json
    db = get_db()
    cursor = db.execute(
        'INSERT INTO planner_blocks (user_id, subject_id, day_of_week, start_hour, end_hour, title) VALUES (?, ?, ?, ?, ?, ?)',
        (current_user.id, data.get('subject_id'), data.get('day_of_week'), data.get('start_hour'), 
         data.get('end_hour', data.get('start_hour') + 1), data.get('title', ''))
    )
    db.commit()
    return jsonify({'success': True, 'id': cursor.lastrowid})


@app.route('/api/planner/<int:id>', methods=['DELETE'])
@login_required
def api_delete_planner_block(id):
    db = get_db()
    db.execute('DELETE FROM planner_blocks WHERE id = ? AND user_id = ?', (id, current_user.id))
    db.commit()
    return jsonify({'success': True})


# --- Analytics ---
@app.route('/api/analytics', methods=['GET'])
@login_required
def api_analytics():
    db = get_db()

    # Hours by subject
    by_subject = db.execute('''
        SELECT s.name, s.color, COALESCE(SUM(ss.duration_minutes), 0) / 60.0 as hours
        FROM subjects s LEFT JOIN study_sessions ss ON s.id = ss.subject_id
        WHERE s.user_id = ?
        GROUP BY s.id ORDER BY hours DESC
    ''', (current_user.id,)).fetchall()

    # Daily trend (last 30 days)
    daily = []
    for i in range(29, -1, -1):
        day = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
        label = (datetime.now() - timedelta(days=i)).strftime('%d %b')
        h = db.execute(
            'SELECT COALESCE(SUM(duration_minutes), 0) / 60.0 as h FROM study_sessions WHERE user_id = ? AND DATE(created_at) = ?',
            (current_user.id, day,)
        ).fetchone()['h']
        daily.append({'date': label, 'hours': round(h, 1)})

    # Sessions by type
    by_type = db.execute('''
        SELECT session_type, COUNT(*) as count, SUM(duration_minutes) / 60.0 as hours
        FROM study_sessions WHERE user_id = ? GROUP BY session_type
    ''', (current_user.id,)).fetchall()

    # Productivity by hour
    by_hour = db.execute('''
        SELECT CAST(strftime('%H', created_at) AS INTEGER) as hour,
               COALESCE(SUM(duration_minutes), 0) / 60.0 as hours
        FROM study_sessions WHERE user_id = ? GROUP BY hour ORDER BY hour
    ''', (current_user.id,)).fetchall()

    return jsonify({
        'by_subject': [dict(s) for s in by_subject],
        'daily_trend': daily,
        'by_type': [dict(t) for t in by_type],
        'by_hour': [dict(h) for h in by_hour]
    })


# ===================== MAIN =====================

if __name__ == '__main__':
    init_db()
    print("\n  [*] Smart Study Planner running at http://localhost:5001\n")
    if ai_client_active:
        print("  [AI] Suggestions: Powered by Groq LLaMA 3\n")
    else:
        print("  [AI] Suggestions: Smart Algorithm Mode")
        print("  [!] Set GROQ_API_KEY env variable for LLaMA AI\n")
    app.run(debug=True, port=5001)
