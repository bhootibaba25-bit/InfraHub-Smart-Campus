import os
import sqlite3
import json
import threading
import time
import random
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv
from google import genai
from flask_mail import Mail, Message

# ==========================================
# 1. SECURE SETUP & EMAIL CONFIGURATION
# ==========================================
load_dotenv()
API_KEY = os.getenv("GEMINI_API_KEY")
app = Flask(__name__)
CORS(app)
client = genai.Client(api_key=API_KEY) if API_KEY else None

app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.getenv("MAIL_USERNAME", "bhandare.sarthak.25@gmail.com")
app.config['MAIL_PASSWORD'] = os.getenv("MAIL_PASSWORD", "ajmpulyzlxxexshd")
mail = Mail(app)

otp_store = {}

# ==========================================
# 2. DATABASE ARCHITECTURE (PRODUCTION MODE)
# ==========================================
def init_db():
    conn = sqlite3.connect('campus_hub.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, custom_id TEXT, name TEXT, email TEXT UNIQUE, password TEXT, role TEXT, mobile_no TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS technicians (technician_id INTEGER PRIMARY KEY AUTOINCREMENT, custom_id TEXT, name TEXT, department TEXT, current_active_hours INTEGER DEFAULT 0, max_shift_hours INTEGER DEFAULT 8, is_on_shift BOOLEAN DEFAULT 1, mobile_no TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS tickets (ticket_id TEXT PRIMARY KEY, user_name TEXT, role TEXT, department TEXT, building TEXT, location TEXT, issue TEXT, photo_attached TEXT, priority TEXT, ai_analysis TEXT, assigned_technician TEXT DEFAULT 'Unassigned', status TEXT DEFAULT 'Pending', decline_reason TEXT, read_status INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS system_notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, target_user TEXT, target_role TEXT, message TEXT, is_urgent INTEGER DEFAULT 0, is_read INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    try:
        c.execute("UPDATE tickets SET department = 'IT & Network Services' WHERE department = 'Technology Services'")
        c.execute("UPDATE tickets SET department = 'Equipment Support' WHERE department = 'Laboratory Equipment Support'")
    except Exception as e:
        pass
        
    conn.commit()
    conn.close()

def get_db_connection():
    conn = sqlite3.connect('campus_hub.db')
    conn.row_factory = sqlite3.Row
    return conn

def add_notification(target_user, target_role, message, is_urgent=0):
    try:
        conn = get_db_connection()
        conn.execute('INSERT INTO system_notifications (target_user, target_role, message, is_urgent) VALUES (?, ?, ?, ?)', (target_user, target_role, message, is_urgent))
        conn.commit()
        conn.close()
    except Exception as e:
        pass

# ==========================================
# 3. AGENTIC TOOLS & QUEUE MANAGEMENT
# ==========================================
def tool_get_available_technician(department, estimated_task_hours=2):
    conn = get_db_connection()
    # AI smartly picks the technician with the LOWEST total backlog (Active + Queued)
    available_techs = conn.execute('''
        SELECT t.name, 
               (SELECT COUNT(*) FROM tickets WHERE assigned_technician = t.name AND status IN ('Assigned', 'In Progress', 'Pending')) as total_tasks
        FROM technicians t
        WHERE (t.department = ? OR t.department LIKE ?) AND t.is_on_shift = 1
        ORDER BY total_tasks ASC
    ''', (department, f'%{department}%')).fetchall()
    conn.close()
    
    if available_techs: 
        return {"status": "success", "technician_name": available_techs[0]['name']}
    return {"status": "error"}

def tool_assign_ticket(ticket_id, technician_name, estimated_task_hours=2):
    conn = get_db_connection()
    tech = conn.execute("SELECT current_active_hours, max_shift_hours FROM technicians WHERE name = ?", (technician_name,)).fetchone()
    
    # If they have room today, assign it. If they are full, put it in their personal queue!
    max_hrs = tech['max_shift_hours'] if tech and tech['max_shift_hours'] else 8
    current_hrs = tech['current_active_hours'] if tech and tech['current_active_hours'] else 0
    
    if tech and (current_hrs + estimated_task_hours) <= max_hrs:
        conn.execute("UPDATE tickets SET assigned_technician = ?, status = 'Assigned' WHERE ticket_id = ?", (technician_name, ticket_id))
    else:
        conn.execute("UPDATE tickets SET assigned_technician = ?, status = 'Pending' WHERE ticket_id = ?", (technician_name, ticket_id))
        print(f"🤖 [AI ROUTING] {technician_name} is full for today. {ticket_id} added to their queue.")
        
    conn.commit()
    conn.close()
    return {"status": "success"}

# ==========================================
# 4. CLASSIFICATION AGENT (AI LOGIC)
# ==========================================
def classify_ticket_with_ai(issue):
    issue_lower = issue.lower()
    urgent_keywords = ['burst', 'flood', 'fire', 'smoke', 'spark', 'gas', 'biohazard', 'blood', 'sewage', 'collapse', 'total outage', 'blackout', 'trapped']
    high_keywords = ['offline', 'entire floor', 'all network', 'spill', 'broken window', 'stuck elevator', 'fume hood', 'server room', 'no power', 'lecture hall']
    
    if any(keyword in issue_lower for keyword in urgent_keywords): 
        priority = 'Urgent'
        analysis = "CRITICAL EMERGENCY: Severe safety, structural, or systemic threat detected. Immediate dispatch required."
    elif any(keyword in issue_lower for keyword in high_keywords): 
        priority = 'High'
        analysis = "HIGH PRIORITY: Significant operational disruption or localized hazard detected. Expedited dispatch recommended."
    else: 
        priority = 'Medium'
        analysis = "STANDARD TICKET: Routine maintenance or localized issue. Assigned to standard SLA queue."

    department = 'Civil Maintenance'
    if any(k in issue_lower for k in ['water', 'leak', 'plumbing', 'pipe', 'flood', 'burst', 'sewage', 'drain', 'faucet', 'toilet']): department = 'Plumbing Maintenance'
    elif any(k in issue_lower for k in ['ac ', 'air conditioning', 'ventilation', 'hot', 'cold', 'hvac']): department = 'Air Conditioning & Ventilation Services'
    elif any(k in issue_lower for k in ['power', 'electrical', 'spark', 'light', 'wire', 'outlet', 'breaker']): department = 'Electrical Maintenance'
    elif any(k in issue_lower for k in ['network', 'server', 'wi-fi', 'internet', 'computer', 'printer']): department = 'IT & Network Services'
    elif any(k in issue_lower for k in ['lock', 'security', 'camera', 'door stuck', 'badge', 'alarm']): department = 'Security & Surveillance'
    elif any(k in issue_lower for k in ['lab', 'microscope', 'centrifuge', 'fume hood', 'incubator', 'equipment']): department = 'Equipment Support'
    elif any(k in issue_lower for k in ['spill', 'trash', 'clean', 'blood', 'vomit', 'biohazard', 'paper towel']): department = 'Housekeeping Services'
        
    return {"department": department, "priority": priority, "ai_analysis": analysis}

# ==========================================
# 5. REST API ROUTES & ENDPOINTS
# ==========================================
@app.route('/<path:filename>')
def serve_static_file(filename): return send_from_directory('.', filename)

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    try:
        conn = get_db_connection()
        conn.execute('INSERT INTO users (name, email, password, role) VALUES (?, ?, ?, ?)', (data['name'], data['email'], data['password'], data['role']))
        
        if data['role'] == 'Campus Technician':
            dept = data.get('department', 'Pending Assignment')
            conn.execute('INSERT INTO technicians (name, department, current_active_hours, max_shift_hours, is_on_shift) VALUES (?, ?, ?, ?, ?)', (data['name'], dept, 0, 8, 1))       
        conn.commit()
        conn.close()
        add_notification(data['name'], None, f"Welcome to InfraHub! Your {data['role']} account is set up and ready.")
        return jsonify({"status": "success", "message": "User registered successfully"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    email = data.get('email', '').strip().lower()
    password = data.get('password', '').strip()
    
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE LOWER(TRIM(email)) = ? AND password = ?', (email, password)).fetchone()
    conn.close()
    
    if user: 
        add_notification(user['name'], None, f"Session Started: Welcome back, {user['name']}.")
        return jsonify({"status": "success", "name": user['name'], "role": user['role']}), 200
    else:
        return jsonify({"status": "error", "message": "Invalid email or password."}), 401

@app.route('/api/forgot-password', methods=['POST'])
def forgot_password():
    email = request.json.get('email')
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE LOWER(email) = LOWER(?)', (email,)).fetchone()
    conn.close()
    if not user: return jsonify({"status": "error", "message": "Email not found in our database."})
    
    otp = str(random.randint(100000, 999999))
    otp_store[email.lower()] = otp
    try:
        msg = Message("InfraHub OTP Reset", sender=app.config['MAIL_USERNAME'], recipients=[email])
        msg.body = f"Your password reset OTP is: {otp}"
        mail.send(msg)
        return jsonify({"status": "success", "message": "OTP sent successfully."})
    except Exception as e:
        print(f"\n[DEBUG] Password Reset OTP for {email} is: {otp}\n")
        return jsonify({"status": "success", "message": "OTP generated (Check Terminal)."})

@app.route('/api/reset-password', methods=['POST'])
def reset_password():
    data = request.json
    email = data.get('email').lower()
    otp = data.get('otp')
    new_password = data.get('new_password')
    if email in otp_store and otp_store[email] == otp:
        try:
            conn = get_db_connection()
            conn.execute('UPDATE users SET password = ? WHERE LOWER(email) = ?', (new_password, email))
            conn.commit()
            user = conn.execute('SELECT * FROM users WHERE LOWER(email) = ?', (email,)).fetchone()
            conn.close()
            del otp_store[email]
            if user: add_notification(user['name'], None, "Security Alert: Your password was recently changed.")
            return jsonify({"status": "success", "message": "Password reset successfully."})
        except Exception as e: return jsonify({"status": "error", "message": "Database error during reset."}), 500
    else: return jsonify({"status": "error", "message": "Invalid or expired OTP."}), 400

@app.route('/api/users', methods=['GET'])
def get_users():
    try:
        conn = get_db_connection()
        users = conn.execute('SELECT * FROM users ORDER BY role, name').fetchall()
        conn.close()
        return jsonify({"status": "success", "data": [dict(u) for u in users]}), 200
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/technicians', methods=['GET'])
def get_technicians():
    try:
        conn = get_db_connection()
        techs = conn.execute('SELECT * FROM technicians ORDER BY department, name').fetchall()
        conn.close()
        return jsonify({"status": "success", "data": [dict(t) for t in techs]}), 200
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/tickets', methods=['POST'])
def create_ticket():
    data = request.json
    ai_decision = classify_ticket_with_ai(data['issue'])
    try:
        conn = get_db_connection()
        conn.execute('INSERT INTO tickets (ticket_id, user_name, role, department, building, location, issue, priority, ai_analysis) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', (data['ticket_id'], data['user_name'], data['role'], ai_decision['department'], data['building'], data['location'], data['issue'], ai_decision['priority'], ai_decision['ai_analysis']))
        conn.commit()
        conn.close()
        
        add_notification(data['user_name'], None, f"Request {data['ticket_id']} successfully sent. AI is analyzing your issue.")
        time.sleep(0.5)
        tech = tool_get_available_technician(ai_decision['department'])
        if tech['status'] == 'success':
            tool_assign_ticket(data['ticket_id'], tech['technician_name'])
            add_notification(tech['technician_name'], None, f"AI DISPATCH: {data['ticket_id']} routed to your tracking portal.", is_urgent=1 if ai_decision['priority'] == 'Urgent' else 0)
            add_notification(data['user_name'], None, f"Update: Ticket {data['ticket_id']} routed to Technician {tech['technician_name']}.")
        else:
            add_notification(None, 'Portal Admin', f"WARNING: {data['ticket_id']} could not be assigned. No techs in {ai_decision['department']}!", is_urgent=1)
        return jsonify({"status": "success"}), 201
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/tickets', methods=['GET'])
def get_tickets():
    try:
        role = request.args.get('role')
        user_name = request.args.get('name')
        conn = get_db_connection()
        
        if role == 'Portal Admin':
            tickets = conn.execute("SELECT * FROM tickets WHERE status != 'Cancelled' ORDER BY created_at DESC").fetchall()
        elif role == 'Campus Technician':
            tickets = conn.execute("SELECT * FROM tickets WHERE assigned_technician = ? AND status != 'Cancelled' ORDER BY created_at DESC", (user_name,)).fetchall()
        else: 
            tickets = conn.execute("SELECT * FROM tickets WHERE user_name = ? AND status != 'Cancelled' ORDER BY created_at DESC", (user_name,)).fetchall()
            
        conn.close()
        return jsonify({"status": "success", "data": [dict(t) for t in tickets]}), 200
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/tickets/<ticket_id>', methods=['DELETE'])
def delete_ticket(ticket_id):
    try:
        conn = get_db_connection()
        conn.execute("UPDATE tickets SET status = 'Cancelled' WHERE ticket_id = ?", (ticket_id,))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"}), 200
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/tickets/<ticket_id>/accept', methods=['PUT'])
def accept(ticket_id):
    try:
        conn = get_db_connection()
        ticket = conn.execute('SELECT * FROM tickets WHERE ticket_id = ?', (ticket_id,)).fetchone()
        conn.execute("UPDATE tickets SET status = 'In Progress' WHERE ticket_id = ?", (ticket_id,))
        conn.commit()
        conn.close()
        if ticket: add_notification(ticket['user_name'], None, f"IN PROGRESS: Technician has started working on {ticket_id}.")
        return jsonify({"status": "success"}), 200
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/tickets/<ticket_id>/resolve', methods=['PUT'])
def resolve(ticket_id):
    try:
        conn = get_db_connection()
        ticket = conn.execute('SELECT * FROM tickets WHERE ticket_id = ?', (ticket_id,)).fetchone()
        conn.execute("UPDATE tickets SET status = 'Resolved' WHERE ticket_id = ?", (ticket_id,))
        conn.commit()
        conn.close()
        if ticket:
            add_notification(ticket['user_name'], None, f"RESOLVED: Your ticket {ticket_id} has been successfully completed!")
            add_notification(None, 'Portal Admin', f"Task Completed: {ticket_id} resolved by {ticket['assigned_technician']}.")
        return jsonify({"status": "success"}), 200
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/tickets/<ticket_id>/decline', methods=['PUT'])
def decline_ticket(ticket_id):
    try:
        data = request.json
        reason = data.get('reason', 'No reason provided')
        conn = get_db_connection()
        ticket = conn.execute('SELECT * FROM tickets WHERE ticket_id = ?', (ticket_id,)).fetchone()
        conn.execute("UPDATE tickets SET status = 'Decline Requested', decline_reason = ? WHERE ticket_id = ?", (reason, ticket_id))
        conn.commit()
        conn.close()
        if ticket:
            add_notification(None, 'Portal Admin', f"ACTION REQUIRED: Tech {ticket['assigned_technician']} requested to decline {ticket_id}.", is_urgent=1)
            add_notification(ticket['user_name'], None, f"Update: Processing reassignment for {ticket_id} to ensure fastest resolution.")
        return jsonify({"status": "success"}), 200
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/tickets/<ticket_id>/approve_decline', methods=['PUT'])
def approve_decline(ticket_id):
    try:
        conn = get_db_connection()
        ticket = conn.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if ticket:
            old_tech = ticket['assigned_technician']
            dept = ticket['department']
            
            new_techs = conn.execute('''
                SELECT t.name, 
                       (SELECT COUNT(*) FROM tickets WHERE assigned_technician = t.name AND status IN ('Assigned', 'In Progress', 'Pending')) as total_tasks
                FROM technicians t
                WHERE (t.department = ? OR t.department LIKE ?) AND t.is_on_shift = 1 AND t.name != ?
                ORDER BY total_tasks ASC
            ''', (dept, f'%{dept}%', old_tech)).fetchall()
            
            if new_techs:
                new_tech_name = new_techs[0]['name']
                conn.execute("UPDATE tickets SET assigned_technician = ?, status = 'Pending', decline_reason = NULL WHERE ticket_id = ?", (new_tech_name, ticket_id))
                add_notification(old_tech, None, f"Admin Approved: You have been removed from ticket {ticket_id}.")
                add_notification(new_tech_name, None, f"REASSIGNED TASK: {ticket_id} has been added to your queue.")
                add_notification(ticket['user_name'], None, f"Update: {ticket_id} reassigned to {new_tech_name}.")
            else:
                conn.execute("UPDATE tickets SET assigned_technician = 'Unassigned', status = 'Pending', decline_reason = NULL WHERE ticket_id = ?", (ticket_id,))
                add_notification(None, 'Portal Admin', f"Unassigned: {ticket_id} returned to queue. No tech available.", is_urgent=1)
            conn.commit()
        conn.close()
        return jsonify({"status": "success"}), 200
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/tickets/<ticket_id>/reject_decline', methods=['PUT'])
def reject_decline(ticket_id):
    try:
        conn = get_db_connection()
        ticket = conn.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
        conn.execute("UPDATE tickets SET status = 'Assigned', decline_reason = NULL WHERE ticket_id = ?", (ticket_id,))
        conn.commit()
        conn.close()
        if ticket: add_notification(ticket['assigned_technician'], None, f"ADMIN DENIED DECLINE: You are required to complete {ticket_id}.", is_urgent=1)
        return jsonify({"status": "success"}), 200
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/notifications', methods=['GET'])
def get_notifications():
    try:
        role = request.args.get('role')
        name = request.args.get('name')
        conn = get_db_connection()
        if role == 'Portal Admin': notifs = conn.execute("SELECT * FROM system_notifications WHERE target_user = ? OR target_role = 'Portal Admin' ORDER BY id DESC LIMIT 50", (name,)).fetchall()
        else: notifs = conn.execute("SELECT * FROM system_notifications WHERE target_user = ? ORDER BY id DESC LIMIT 50", (name,)).fetchall()
        conn.close()
        return jsonify({"status": "success", "data": [dict(n) for n in notifs]}), 200
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/notifications/read', methods=['POST'])
def mark_read():
    try:
        data = request.json
        conn = get_db_connection()
        conn.execute("UPDATE system_notifications SET is_read = 1 WHERE target_user = ? OR target_role = ?", (data['name'], data['role']))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# 6. CENTRAL AI MONITORING LOOP
# ==========================================
def monitoring_agent_loop():
    time.sleep(2)
    last_reset_date = None
    
    while True:
        try:
            conn = get_db_connection() 
            now = datetime.now()
            current_date = now.date()
            
            # Reset technician active hours daily at 8 AM
            if now.hour >= 8 and now.weekday() < 5 and last_reset_date != current_date:
                conn.execute("UPDATE technicians SET current_active_hours = 0")
                conn.commit()
                last_reset_date = current_date
            
            # 1. ENFORCE SHIFT LIMITS & HEAL CORRUPT DATA
            techs = conn.execute("SELECT name, max_shift_hours FROM technicians").fetchall()
            for t in techs:
                max_hours = t['max_shift_hours'] if t['max_shift_hours'] else 8
                allowed_tasks = max_hours // 2
                
                # Fetch all active tickets for this tech
                active_tickets = conn.execute("SELECT ticket_id FROM tickets WHERE assigned_technician = ? AND status IN ('Assigned', 'In Progress') ORDER BY created_at ASC", (t['name'],)).fetchall()
                
                # If they have too many tasks, Demote the extras to Pending (Queue)
                if len(active_tickets) > allowed_tasks:
                    excess_count = len(active_tickets) - allowed_tasks
                    print(f"🤖 [AI DISPATCHER] {t['name']} is overloaded! Moving {excess_count} tasks to their personal queue.")
                    for excess in active_tickets[allowed_tasks:]:
                        conn.execute("UPDATE tickets SET status = 'Pending' WHERE ticket_id = ?", (excess['ticket_id'],))
                    active_tickets = active_tickets[:allowed_tasks]
                    
                # Update their exact hours based on what is actually active
                actual_hours = len(active_tickets) * 2
                conn.execute("UPDATE technicians SET current_active_hours = ? WHERE name = ?", (actual_hours, t['name']))
                
                # 2. PROMOTE TASKS FROM QUEUE IF THEY HAVE FREE TIME
                if actual_hours + 2 <= max_hours:
                    space_for_tasks = (max_hours - actual_hours) // 2
                    pending_tickets = conn.execute("SELECT ticket_id FROM tickets WHERE assigned_technician = ? AND status = 'Pending' ORDER BY created_at ASC LIMIT ?", (t['name'], space_for_tasks)).fetchall()
                    
                    for pt in pending_tickets:
                        print(f"🤖 [AI DISPATCHER] Free time detected for {t['name']}. Promoting task {pt['ticket_id']} from Queue to Active!")
                        conn.execute("UPDATE tickets SET status = 'Assigned' WHERE ticket_id = ?", (pt['ticket_id'],))
                        conn.execute("UPDATE technicians SET current_active_hours = current_active_hours + 2 WHERE name = ?", (t['name'],))
                        add_notification(t['name'], None, f"QUEUE PROMOTION: Task {pt['ticket_id']} moved to your active workload.")
            
            # 3. ESCALATE UNASSIGNED EMERGENCIES
            overdue = conn.execute("SELECT ticket_id FROM tickets WHERE priority IN ('Urgent', 'High') AND status = 'Pending' AND assigned_technician = 'Unassigned'").fetchall()
            for o in overdue:
                conn.execute("UPDATE tickets SET status = 'Escalated' WHERE ticket_id = ?", (o['ticket_id'],))
                add_notification(None, 'Portal Admin', f"ESCALATION: {o['ticket_id']} is critical and unassigned! Intervene.", is_urgent=1)
            
            # 4. ASSIGN BRAND NEW TICKETS
            unassigned = conn.execute("SELECT ticket_id, department FROM tickets WHERE assigned_technician = 'Unassigned' AND status = 'Pending'").fetchall()
            for u in unassigned:
                tech = tool_get_available_technician(u['department'])
                if tech['status'] == 'success':
                    tool_assign_ticket(u['ticket_id'], tech['technician_name'])
                    
            conn.commit()
            conn.close()
        except Exception as e: 
            pass
        
        # Snappy AI updates every 5 seconds
        time.sleep(5)

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

if __name__ == '__main__':
    init_db()
    monitor_thread = threading.Thread(target=monitoring_agent_loop, daemon=True)
    monitor_thread.start()
    app.run(debug=True, port=5005, use_reloader=False)