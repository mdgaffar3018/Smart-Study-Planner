import sqlite3
import os
import json
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, g
from dotenv import load_dotenv

# Load environment variables from .env file if present
load_dotenv()

# --- Google Gemini AI ---
try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

app = Flask(__name__)
app.secret_key = 'smart-study-planner-secret-key-2026'

# Use /tmp for database on Vercel deployment, otherwise local directory
if os.environ.get('VERCEL') == '1' or os.environ.get('VERCEL_ENV'):
    DATABASE = '/tmp/study_planner.db'
else:
    DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'study_planner.db')

# --- Gemini AI Configuration ---
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
gemini_client = None

if GEMINI_AVAILABLE and GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_client = genai.GenerativeModel('gemini-1.5-flash-latest')
    except Exception:
        gemini_client = None


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
        CREATE TABLE IF NOT EXISTS subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            color TEXT DEFAULT '#6C63FF',
            icon TEXT DEFAULT 'fa-book',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_id INTEGER,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            priority TEXT DEFAULT 'medium',
            deadline TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS study_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_id INTEGER,
            duration_minutes INTEGER NOT NULL,
            session_type TEXT DEFAULT 'manual',
            notes TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            target_hours REAL DEFAULT 10,
            current_hours REAL DEFAULT 0,
            deadline TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_id INTEGER,
            title TEXT NOT NULL,
            content TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS planner_blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_id INTEGER,
            day_of_week INTEGER NOT NULL,
            start_hour INTEGER NOT NULL,
            end_hour INTEGER NOT NULL,
            title TEXT DEFAULT '',
            FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS user_profile (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            xp INTEGER DEFAULT 0,
            level INTEGER DEFAULT 1
        );
        INSERT INTO user_profile (id, xp, level) 
        SELECT 1, 0, 1 WHERE NOT EXISTS (SELECT 1 FROM user_profile WHERE id=1);
    ''')


# ===================== AI SUGGESTIONS =====================

def get_study_context(db):
    """Gather current study data for AI context."""
    subjects = db.execute('SELECT * FROM subjects').fetchall()
    tasks = db.execute('SELECT t.*, s.name as subject_name FROM tasks t LEFT JOIN subjects s ON t.subject_id = s.id ORDER BY deadline').fetchall()
    sessions = db.execute('''
        SELECT ss.*, s.name as subject_name 
        FROM study_sessions ss 
        LEFT JOIN subjects s ON ss.subject_id = s.id 
        ORDER BY ss.created_at DESC LIMIT 20
    ''').fetchall()
    goals = db.execute('SELECT * FROM goals WHERE status = "active"').fetchall()

    # Calculate study hours per subject
    hours_per_subject = db.execute('''
        SELECT s.name, COALESCE(SUM(ss.duration_minutes), 0) / 60.0 as hours
        FROM subjects s
        LEFT JOIN study_sessions ss ON s.id = ss.subject_id
        GROUP BY s.id
    ''').fetchall()

    # Recent 7-day totals
    week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    weekly_hours = db.execute('''
        SELECT COALESCE(SUM(duration_minutes), 0) / 60.0 as hours
        FROM study_sessions WHERE created_at >= ?
    ''', (week_ago,)).fetchone()['hours']

    context = {
        'subjects': [dict(s) for s in subjects],
        'pending_tasks': [dict(t) for t in tasks if t['status'] == 'pending'],
        'completed_tasks_count': len([t for t in tasks if t['status'] == 'completed']),
        'hours_per_subject': {row['name']: round(row['hours'], 1) for row in hours_per_subject},
        'weekly_study_hours': round(weekly_hours, 1),
        'active_goals': [dict(g) for g in goals],
        'recent_sessions_count': len(sessions),
        'total_sessions': db.execute('SELECT COUNT(*) as c FROM study_sessions').fetchone()['c'],
        'profile': dict(db.execute('SELECT * FROM user_profile WHERE id=1').fetchone())
    }
    return context


def get_ai_suggestions(db):
    """Get AI-powered study suggestions using Gemini or fallback."""
    context = get_study_context(db)

    if gemini_client:
        return get_gemini_suggestions(context)
    else:
        return get_smart_fallback_suggestions(context)


def get_gemini_suggestions(context):
    """Use Google Gemini to generate study suggestions."""
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
        response = gemini_client.generate_content(prompt)
        text = response.text.strip()
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
def dashboard():
    return render_template('dashboard.html', active='dashboard')


@app.route('/subjects')
def subjects_page():
    return render_template('subjects.html', active='subjects')


@app.route('/tasks')
def tasks_page():
    return render_template('tasks.html', active='tasks')


@app.route('/timer')
def timer_page():
    return render_template('timer.html', active='timer')


@app.route('/analytics')
def analytics_page():
    return render_template('analytics.html', active='analytics')


@app.route('/planner')
def planner_page():
    return render_template('planner.html', active='planner')


@app.route('/notes')
def notes_page():
    return render_template('notes.html', active='notes')


# ===================== API ROUTES =====================

# --- Gamification / Profile ---
def award_xp(db, xp_amount):
    """Awards XP to the user and calculates level."""
    profile = db.execute('SELECT * FROM user_profile WHERE id=1').fetchone()
    if profile:
        new_xp = profile['xp'] + xp_amount
        new_level = (new_xp // 500) + 1  # 500 XP per level
        db.execute('UPDATE user_profile SET xp=?, level=? WHERE id=1', (new_xp, new_level))
        
@app.route('/api/profile')
def api_profile():
    db = get_db()
    profile = db.execute('SELECT * FROM user_profile WHERE id=1').fetchone()
    return jsonify(dict(profile))

# --- Stats ---
@app.route('/api/stats')
def api_stats():
    db = get_db()
    total_hours = db.execute('SELECT COALESCE(SUM(duration_minutes), 0) / 60.0 as h FROM study_sessions').fetchone()['h']
    tasks_done = db.execute('SELECT COUNT(*) as c FROM tasks WHERE status = "completed"').fetchone()['c']
    tasks_total = db.execute('SELECT COUNT(*) as c FROM tasks').fetchone()['c']
    subjects_count = db.execute('SELECT COUNT(*) as c FROM subjects').fetchone()['c']
    sessions_count = db.execute('SELECT COUNT(*) as c FROM study_sessions').fetchone()['c']

    # Weekly data (last 7 days)
    weekly = []
    for i in range(6, -1, -1):
        day = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
        day_label = (datetime.now() - timedelta(days=i)).strftime('%a')
        hours = db.execute(
            'SELECT COALESCE(SUM(duration_minutes), 0) / 60.0 as h FROM study_sessions WHERE DATE(created_at) = ?',
            (day,)
        ).fetchone()['h']
        weekly.append({'day': day_label, 'hours': round(hours, 1)})

    # Streak calculation
    streak = 0
    for i in range(0, 60):
        day = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
        count = db.execute('SELECT COUNT(*) as c FROM study_sessions WHERE DATE(created_at) = ?', (day,)).fetchone()['c']
        if count > 0:
            streak += 1
        else:
            if i > 0:
                break

    # Upcoming deadlines
    upcoming = db.execute('''
        SELECT t.*, s.name as subject_name, s.color as subject_color
        FROM tasks t LEFT JOIN subjects s ON t.subject_id = s.id
        WHERE t.status = 'pending' AND t.deadline IS NOT NULL
        ORDER BY t.deadline LIMIT 5
    ''').fetchall()

    # Recent sessions
    recent_sessions = db.execute('''
        SELECT ss.*, s.name as subject_name, s.color as subject_color
        FROM study_sessions ss LEFT JOIN subjects s ON ss.subject_id = s.id
        ORDER BY ss.created_at DESC LIMIT 5
    ''').fetchall()

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
def api_suggestions():
    db = get_db()
    suggestions = get_ai_suggestions(db)
    ai_powered = bool(gemini_client)
    return jsonify({'suggestions': suggestions, 'ai_powered': ai_powered})


# --- AI Chatbot ---
@app.route('/api/chat', methods=['POST'])
def api_chat():
    data = request.json
    user_message = data.get('message', '')
    
    if not gemini_client:
        return jsonify({'reply': 'Sorry, the AI Buddy is currently offline. Please set the GEMINI_API_KEY environment variable.'})
        
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
        response = gemini_client.generate_content(prompt)
        return jsonify({'reply': response.text.strip()})
    except Exception as e:
        return jsonify({'reply': f'Sorry, I encountered an error checking my circuits. {str(e)}'})

# --- Subjects ---
@app.route('/api/subjects', methods=['GET'])
def api_get_subjects():
    db = get_db()
    subjects = db.execute('''
        SELECT s.*, 
            COALESCE(SUM(ss.duration_minutes), 0) / 60.0 as total_hours,
            COUNT(DISTINCT t.id) as task_count
        FROM subjects s
        LEFT JOIN study_sessions ss ON s.id = ss.subject_id
        LEFT JOIN tasks t ON s.id = t.subject_id
        GROUP BY s.id
        ORDER BY s.name
    ''').fetchall()
    return jsonify([dict(s) for s in subjects])


@app.route('/api/subjects', methods=['POST'])
def api_add_subject():
    db = get_db()
    data = request.json
    db.execute('INSERT INTO subjects (name, color, icon) VALUES (?, ?, ?)',
               (data['name'], data.get('color', '#6C63FF'), data.get('icon', 'fa-book')))
    db.commit()
    return jsonify({'success': True})


@app.route('/api/subjects/<int:id>', methods=['PUT'])
def api_update_subject(id):
    db = get_db()
    data = request.json
    db.execute('UPDATE subjects SET name=?, color=?, icon=? WHERE id=?',
               (data['name'], data.get('color', '#6C63FF'), data.get('icon', 'fa-book'), id))
    db.commit()
    return jsonify({'success': True})


@app.route('/api/subjects/<int:id>', methods=['DELETE'])
def api_delete_subject(id):
    db = get_db()
    db.execute('DELETE FROM subjects WHERE id=?', (id,))
    db.commit()
    return jsonify({'success': True})


# --- Tasks ---
@app.route('/api/tasks', methods=['GET'])
def api_get_tasks():
    db = get_db()
    tasks = db.execute('''
        SELECT t.*, s.name as subject_name, s.color as subject_color
        FROM tasks t LEFT JOIN subjects s ON t.subject_id = s.id
        ORDER BY 
            CASE t.status WHEN 'pending' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END,
            CASE t.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
            t.deadline
    ''').fetchall()
    return jsonify([dict(t) for t in tasks])


@app.route('/api/tasks', methods=['POST'])
def api_add_task():
    db = get_db()
    data = request.json
    db.execute('INSERT INTO tasks (subject_id, title, description, priority, deadline, status) VALUES (?,?,?,?,?,?)',
               (data.get('subject_id'), data['title'], data.get('description', ''),
                data.get('priority', 'medium'), data.get('deadline'), data.get('status', 'pending')))
    db.commit()
    return jsonify({'success': True})


@app.route('/api/tasks/<int:id>', methods=['PUT'])
def api_update_task(id):
    db = get_db()
    data = request.json
    db.execute('UPDATE tasks SET subject_id=?, title=?, description=?, priority=?, deadline=?, status=? WHERE id=?',
               (data.get('subject_id'), data['title'], data.get('description', ''),
                data.get('priority', 'medium'), data.get('deadline'), data.get('status', 'pending'), id))
    db.commit()
    return jsonify({'success': True})


@app.route('/api/tasks/<int:id>', methods=['DELETE'])
def api_delete_task(id):
    db = get_db()
    db.execute('DELETE FROM tasks WHERE id=?', (id,))
    db.commit()
    return jsonify({'success': True})


@app.route('/api/tasks/<int:id>/toggle', methods=['POST'])
def api_toggle_task(id):
    db = get_db()
    task = db.execute('SELECT status FROM tasks WHERE id=?', (id,)).fetchone()
    earned_xp = 0
    if task:
        new_status = 'completed' if task['status'] != 'completed' else 'pending'
        db.execute('UPDATE tasks SET status=? WHERE id=?', (new_status, id))
        
        # Award XP for completing a task
        if new_status == 'completed':
            award_xp(db, 50)
            earned_xp = 50
            
        db.commit()
    return jsonify({'success': True, 'earned_xp': earned_xp})


# --- Study Sessions ---
@app.route('/api/sessions', methods=['GET'])
def api_get_sessions():
    db = get_db()
    sessions = db.execute('''
        SELECT ss.*, s.name as subject_name, s.color as subject_color
        FROM study_sessions ss LEFT JOIN subjects s ON ss.subject_id = s.id
        ORDER BY ss.created_at DESC LIMIT 50
    ''').fetchall()
    return jsonify([dict(s) for s in sessions])


@app.route('/api/sessions', methods=['POST'])
def api_add_session():
    db = get_db()
    data = request.json
    duration = data['duration_minutes']
    
    db.execute('INSERT INTO study_sessions (subject_id, duration_minutes, session_type, notes) VALUES (?,?,?,?)',
               (data.get('subject_id'), duration,
                data.get('session_type', 'manual'), data.get('notes', '')))
    
    # Award XP for study session (10 XP per minute + 50 bonus for finishing Pomodoro)
    earned_xp = int((duration * 10))
    if data.get('session_type') == 'pomodoro':
        earned_xp += 50
    
    award_xp(db, earned_xp)
    db.commit()
    return jsonify({'success': True, 'earned_xp': earned_xp})


@app.route('/api/sessions/<int:id>', methods=['DELETE'])
def api_delete_session(id):
    db = get_db()
    db.execute('DELETE FROM study_sessions WHERE id=?', (id,))
    db.commit()
    return jsonify({'success': True})


# --- Goals ---
@app.route('/api/goals', methods=['GET'])
def api_get_goals():
    db = get_db()
    goals = db.execute('SELECT * FROM goals ORDER BY status, deadline').fetchall()
    return jsonify([dict(g) for g in goals])


@app.route('/api/goals', methods=['POST'])
def api_add_goal():
    db = get_db()
    data = request.json
    db.execute('INSERT INTO goals (title, target_hours, deadline) VALUES (?,?,?)',
               (data['title'], data.get('target_hours', 10), data.get('deadline')))
    db.commit()
    return jsonify({'success': True})


@app.route('/api/goals/<int:id>', methods=['PUT'])
def api_update_goal(id):
    db = get_db()
    data = request.json
    db.execute('UPDATE goals SET title=?, target_hours=?, current_hours=?, deadline=?, status=? WHERE id=?',
               (data['title'], data.get('target_hours', 10), data.get('current_hours', 0),
                data.get('deadline'), data.get('status', 'active'), id))
    db.commit()
    return jsonify({'success': True})


@app.route('/api/goals/<int:id>', methods=['DELETE'])
def api_delete_goal(id):
    db = get_db()
    db.execute('DELETE FROM goals WHERE id=?', (id,))
    db.commit()
    return jsonify({'success': True})


# --- Notes ---
@app.route('/api/notes', methods=['GET'])
def api_get_notes():
    db = get_db()
    notes = db.execute('''
        SELECT n.*, s.name as subject_name, s.color as subject_color
        FROM notes n LEFT JOIN subjects s ON n.subject_id = s.id
        ORDER BY n.updated_at DESC
    ''').fetchall()
    return jsonify([dict(n) for n in notes])


@app.route('/api/notes', methods=['POST'])
def api_add_note():
    db = get_db()
    data = request.json
    db.execute('INSERT INTO notes (subject_id, title, content) VALUES (?,?,?)',
               (data.get('subject_id'), data['title'], data.get('content', '')))
    db.commit()
    return jsonify({'success': True})


@app.route('/api/notes/<int:id>', methods=['PUT'])
def api_update_note(id):
    db = get_db()
    data = request.json
    db.execute('UPDATE notes SET subject_id=?, title=?, content=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
               (data.get('subject_id'), data['title'], data.get('content', ''), id))
    db.commit()
    return jsonify({'success': True})


@app.route('/api/notes/<int:id>', methods=['DELETE'])
def api_delete_note(id):
    db = get_db()
    db.execute('DELETE FROM notes WHERE id=?', (id,))
    db.commit()
    return jsonify({'success': True})


# --- Planner Blocks ---
@app.route('/api/planner', methods=['GET'])
def api_get_planner():
    db = get_db()
    blocks = db.execute('''
        SELECT p.*, s.name as subject_name, s.color as subject_color
        FROM planner_blocks p LEFT JOIN subjects s ON p.subject_id = s.id
        ORDER BY p.day_of_week, p.start_hour
    ''').fetchall()
    return jsonify([dict(b) for b in blocks])


@app.route('/api/planner', methods=['POST'])
def api_add_planner_block():
    db = get_db()
    data = request.json
    db.execute('INSERT INTO planner_blocks (subject_id, day_of_week, start_hour, end_hour, title) VALUES (?,?,?,?,?)',
               (data.get('subject_id'), data['day_of_week'], data['start_hour'],
                data.get('end_hour', data['start_hour'] + 1), data.get('title', '')))
    db.commit()
    return jsonify({'success': True})


@app.route('/api/planner/<int:id>', methods=['DELETE'])
def api_delete_planner_block(id):
    db = get_db()
    db.execute('DELETE FROM planner_blocks WHERE id=?', (id,))
    db.commit()
    return jsonify({'success': True})


# --- Analytics ---
@app.route('/api/analytics')
def api_analytics():
    db = get_db()

    # Hours by subject
    by_subject = db.execute('''
        SELECT s.name, s.color, COALESCE(SUM(ss.duration_minutes), 0) / 60.0 as hours
        FROM subjects s LEFT JOIN study_sessions ss ON s.id = ss.subject_id
        GROUP BY s.id ORDER BY hours DESC
    ''').fetchall()

    # Daily trend (last 30 days)
    daily = []
    for i in range(29, -1, -1):
        day = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
        label = (datetime.now() - timedelta(days=i)).strftime('%d %b')
        h = db.execute(
            'SELECT COALESCE(SUM(duration_minutes), 0) / 60.0 as h FROM study_sessions WHERE DATE(created_at) = ?',
            (day,)
        ).fetchone()['h']
        daily.append({'date': label, 'hours': round(h, 1)})

    # Sessions by type
    by_type = db.execute('''
        SELECT session_type, COUNT(*) as count, SUM(duration_minutes) / 60.0 as hours
        FROM study_sessions GROUP BY session_type
    ''').fetchall()

    # Productivity by hour
    by_hour = db.execute('''
        SELECT CAST(strftime('%H', created_at) AS INTEGER) as hour,
               COALESCE(SUM(duration_minutes), 0) / 60.0 as hours
        FROM study_sessions GROUP BY hour ORDER BY hour
    ''').fetchall()

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
    if gemini_client:
        print("  [AI] Suggestions: Powered by Google Gemini\n")
    else:
        print("  [AI] Suggestions: Smart Algorithm Mode")
        print("  [!] Set GEMINI_API_KEY env variable for Gemini AI\n")
    app.run(debug=True, port=5001)
