import os
import sqlite3
import json
import threading
import time
import random
import csv
from io import StringIO
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
from dotenv import load_dotenv
from google import genai
from google.genai import types
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
# 1.5 EMAIL NOTIFICATION BOT (BACKGROUND THREADS)
# ==========================================
def send_async_email(app_obj, user_email, subject, body):
    with app_obj.app_context():
        try:
            msg = Message(subject, sender=app_obj.config['MAIL_USERNAME'], recipients=[user_email])
            msg.body = body
            mail.send(msg)
        except Exception as e:
            print(f"Email Sending Error: {e}")

def notify_status_change(ticket_id, new_status, db_conn=None):
    """Fetches user email based on ticket_id and triggers a background email thread."""
    conn = db_conn or get_db_connection()
    try:
        # Link the ticket to the user's email address
        query = '''
            SELECT t.user_name, u.email 
            FROM tickets t 
            JOIN users u ON t.user_name = u.name 
            WHERE t.ticket_id = ?
        '''
        user = conn.execute(query, (ticket_id,)).fetchone()
        
        if user and user['email']:
            subject = f"InfraHub Update: Request {ticket_id} is {new_status}"
            body = f"Hello {user['user_name']},\n\nYour campus maintenance request {ticket_id} has been officially updated to: {new_status}.\n\nYou can log in to the InfraHub portal at any time to track its live progress.\n\nBest regards,\nInfraHub AI Dispatcher"
            
            # Fire and forget: send email without making the frontend wait
            threading.Thread(target=send_async_email, args=(app, user['email'], subject, body)).start()
            print(f"⚠️ [DEBUG] Silently skipped email! Could not find email address for ticket: {ticket_id}")
    except Exception as e:
        print("Email Notification DB Error:", e)
    finally:
        if not db_conn and conn:
            conn.close()

# ==========================================
# 2. DATABASE ARCHITECTURE (PRODUCTION MODE)
# ==========================================
def get_db_connection():
    conn = sqlite3.connect('campus_hub.db', timeout=20)
    # WAL Mode drastically improves concurrent database access
    conn.execute('PRAGMA journal_mode=WAL;') 
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    try:
        c = conn.cursor()
        
        # 1. CREATE TABLES FIRST
        c.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, custom_id TEXT, name TEXT, email TEXT UNIQUE, password TEXT, role TEXT, mobile_no TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS technicians (technician_id INTEGER PRIMARY KEY AUTOINCREMENT, custom_id TEXT, name TEXT, department TEXT, current_active_hours INTEGER DEFAULT 0, max_shift_hours INTEGER DEFAULT 8, is_on_shift BOOLEAN DEFAULT 1, mobile_no TEXT, points INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS tickets (ticket_id TEXT PRIMARY KEY, user_name TEXT, role TEXT, department TEXT, building TEXT, location TEXT, issue TEXT, photo_attached TEXT, priority TEXT, ai_analysis TEXT, assigned_technician TEXT DEFAULT 'Unassigned', status TEXT DEFAULT 'Pending', decline_reason TEXT, read_status INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, qa_sent INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS system_notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, target_user TEXT, target_role TEXT, message TEXT, is_urgent INTEGER DEFAULT 0, is_read INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS inventory (id INTEGER PRIMARY KEY AUTOINCREMENT, item_name TEXT, category TEXT, stock_level INTEGER DEFAULT 0, reorder_threshold INTEGER DEFAULT 5, unit_price REAL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS part_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, tech_name TEXT, part_name TEXT, status TEXT DEFAULT 'Pending', requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        c.execute('''CREATE TABLE IF NOT EXISTS audit_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT, action TEXT, target TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        
        # Seed initial inventory if empty
       # ↓↓↓ REPLACED INVENTORY SEED WITH 10 REAL DEPARTMENTS & RUPEES ↓↓↓
        inv_check = c.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
        if inv_check == 0:
            seed_items = [
                ('Heavy Duty PVC Pipe (1 inch)', 'Plumbing Maintenance', 12, 5, 450.00),
                ('Cat6 Ethernet Cable (Box)', 'IT & Network Services', 3, 10, 2500.00),
                ('D-Link Gigabit Switch (8-Port)', 'IT & Network Services', 5, 2, 1250.00),
                ('Industrial AC Filter (Daikin)', 'Air Conditioning & Ventilation Services', 2, 8, 850.00),
                ('Havells 20A Circuit Breaker', 'Electrical Maintenance', 15, 5, 320.00),
                ('Syska LED Panel Light (15W)', 'Electrical Maintenance', 4, 10, 550.00),
                ('Ambuja Portland Cement (50kg)', 'Civil Maintenance', 20, 10, 410.00),
                ('CP Plus Dome CCTV Camera', 'Security & Surveillance', 6, 4, 1850.00),
                ('Diversey Floor Cleaner (5L)', 'Housekeeping Services', 8, 3, 650.00),
                ('Ceasefire ABC Extinguisher (4kg)', 'Fire Safety Systems', 5, 2, 2100.00),
                ('Crompton Submersible Pump (1HP)', 'Water Supply & Sewage Management', 1, 1, 8500.00),
                ('Fluke Digital Multimeter', 'Equipment Support', 3, 2, 3400.00)
            ]
            c.executemany("INSERT INTO inventory (item_name, category, stock_level, reorder_threshold, unit_price) VALUES (?, ?, ?, ?, ?)", seed_items)
        # ↑↑↑ END OF REPLACED INVENTORY SEED ↑↑↑
        try:
            c.execute("ALTER TABLE tickets ADD COLUMN qa_sent INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE technicians ADD COLUMN points INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE tickets ADD COLUMN started_at TIMESTAMP")
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE tickets ADD COLUMN time_taken_mins INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        # 3. RUN UPDATES
        c.execute("UPDATE tickets SET department = 'IT & Network Services' WHERE department = 'Technology Services'")
        c.execute("UPDATE tickets SET department = 'Equipment Support' WHERE department = 'Laboratory Equipment Support'")
        
        conn.commit()
    except Exception as e:
        print("Init DB Error:", e)
    finally:
        conn.close()
def log_audit(user, action, target, db_conn=None):
    conn = db_conn or get_db_connection()
    try:
        conn.execute('INSERT INTO audit_logs (user, action, target) VALUES (?, ?, ?)', (user, action, target))
        if not db_conn: 
            conn.commit()
    except Exception as e:
        print("Audit Log Error:", e)
    finally:
        if not db_conn and conn: 
            conn.close()
        

def add_notification(target_user, target_role, message, is_urgent=0, db_conn=None):
    # Uses existing connection to prevent deadlocks
    conn = db_conn or get_db_connection()
    try:
        conn.execute('INSERT INTO system_notifications (target_user, target_role, message, is_urgent) VALUES (?, ?, ?, ?)', (target_user, target_role, message, is_urgent))
        if not db_conn:
            conn.commit()
    except Exception as e:
        print("Notification Error:", e)
    finally:
        if not db_conn and conn:
            conn.close()

# ==========================================
# 3. AGENTIC TOOLS & QUEUE MANAGEMENT
# ==========================================
def tool_get_available_technician(department, estimated_task_hours=2, db_conn=None):
    conn = db_conn or get_db_connection()
    try:
        available_techs = conn.execute('''
            SELECT t.name, 
                   (SELECT COUNT(*) FROM tickets WHERE assigned_technician = t.name AND status IN ('Assigned', 'In Progress', 'Pending')) as total_tasks
            FROM technicians t
            WHERE (t.department = ? OR t.department LIKE ?) AND t.is_on_shift = 1
            ORDER BY total_tasks ASC
        ''', (department, f'%{department}%')).fetchall()
        
        if available_techs: 
            return {"status": "success", "technician_name": available_techs[0]['name']}
        return {"status": "error"}
    finally:
        if not db_conn and conn:
            conn.close()

def tool_assign_ticket(ticket_id, technician_name, estimated_task_hours=2, db_conn=None):
    conn = db_conn or get_db_connection()
    try:
        tech = conn.execute("SELECT current_active_hours, max_shift_hours FROM technicians WHERE name = ?", (technician_name,)).fetchone()
        
        max_hrs = tech['max_shift_hours'] if tech and tech['max_shift_hours'] else 8
        current_hrs = tech['current_active_hours'] if tech and tech['current_active_hours'] else 0
        
        if tech and (current_hrs + estimated_task_hours) <= max_hrs:
            conn.execute("UPDATE tickets SET assigned_technician = ?, status = 'Assigned' WHERE ticket_id = ?", (technician_name, ticket_id))
            notify_status_change(ticket_id, 'Assigned', db_conn=conn)
        else:
            conn.execute("UPDATE tickets SET assigned_technician = ?, status = 'Pending' WHERE ticket_id = ?", (technician_name, ticket_id))
            print(f"🤖 [AI ROUTING] {technician_name} is full for today. {ticket_id} added to their queue.")
            
        if not db_conn:
            conn.commit()
        return {"status": "success"}
    finally:
        if not db_conn and conn:
            conn.close()

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
def serve_static_file(filename): 
    return send_from_directory('.', filename)

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    conn = get_db_connection()
    try:
        conn.execute('INSERT INTO users (name, email, password, role) VALUES (?, ?, ?, ?)', (data['name'], data['email'], data['password'], data['role']))
        
        if data['role'] == 'Campus Technician':
            dept = data.get('department', 'Pending Assignment')
            conn.execute('INSERT INTO technicians (name, department, current_active_hours, max_shift_hours, is_on_shift) VALUES (?, ?, ?, ?, ?)', (data['name'], dept, 0, 8, 1))       
        
        add_notification(data['name'], None, f"Welcome to InfraHub! Your {data['role']} account is set up and ready.", db_conn=conn)
        conn.commit()
        return jsonify({"status": "success", "message": "User registered successfully"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    finally:
        conn.close()
@app.route('/api/export/tickets', methods=['GET'])
def export_tickets():
    conn = get_db_connection()
    try:
        # Upgraded query to pull tech hours and simulate costs
        tickets = conn.execute("""
            SELECT t.ticket_id, t.user_name, t.department, t.issue, t.status, t.time_taken_mins, 
                   tech.current_active_hours, t.created_at
            FROM tickets t 
            LEFT JOIN technicians tech ON t.assigned_technician = tech.name
            ORDER BY t.created_at DESC
        """).fetchall()
        
        si = StringIO()
        cw = csv.writer(si)
        cw.writerow(['Ticket ID', 'User', 'Department', 'Issue', 'Status', 'Resolution Time (Mins)', 'Tech Total Hours Today', 'Date'])
        
        for t in tickets:
            cw.writerow([t['ticket_id'], t['user_name'], t['department'], t['issue'], t['status'], t['time_taken_mins'], t['current_active_hours'], t['created_at']])
        
        output = si.getvalue()
        return Response(output, mimetype="text/csv", headers={"Content-Disposition": "attachment;filename=InfraHub_Finance_Report.csv"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500
    finally: conn.close()

@app.route('/api/analytics', methods=['GET'])
def get_analytics():
    conn = get_db_connection()
    try:
        # Group tickets by Department
        dept_data = conn.execute("SELECT department, COUNT(*) as count FROM tickets GROUP BY department").fetchall()
        # Group tickets by Status
        status_data = conn.execute("SELECT status, COUNT(*) as count FROM tickets GROUP BY status").fetchall()
        
        return jsonify({
            "status": "success",
            "departments": [{"name": d['department'], "count": d['count']} for d in dept_data],
            "statuses": [{"name": s['status'], "count": s['count']} for s in status_data]
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()    

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    email = data.get('email', '').strip().lower()
    password = data.get('password', '').strip()
    
    conn = get_db_connection()
    try:
        user = conn.execute('SELECT * FROM users WHERE LOWER(TRIM(email)) = ? AND password = ?', (email, password)).fetchone()
        if user: 
            add_notification(user['name'], None, f"Session Started: Welcome back, {user['name']}.", db_conn=conn)
            conn.commit()
            return jsonify({"status": "success", "name": user['name'], "role": user['role']}), 200
        else:
            return jsonify({"status": "error", "message": "Invalid email or password."}), 401
    finally:
        conn.close()

@app.route('/api/forgot-password', methods=['POST'])
def forgot_password():
    email = request.json.get('email')
    conn = get_db_connection()
    try:
        user = conn.execute('SELECT * FROM users WHERE LOWER(email) = LOWER(?)', (email,)).fetchone()
        if not user: 
            return jsonify({"status": "error", "message": "Email not found in our database."})
        
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
    finally:
        conn.close()

@app.route('/api/reset-password', methods=['POST'])
def reset_password():
    data = request.json
    email = data.get('email').lower()
    otp = data.get('otp')
    new_password = data.get('new_password')
    if email in otp_store and otp_store[email] == otp:
        conn = get_db_connection()
        try:
            conn.execute('UPDATE users SET password = ? WHERE LOWER(email) = ?', (new_password, email))
            user = conn.execute('SELECT * FROM users WHERE LOWER(email) = ?', (email,)).fetchone()
            if user: 
                add_notification(user['name'], None, "Security Alert: Your password was recently changed.", db_conn=conn)
            conn.commit()
            del otp_store[email]
            return jsonify({"status": "success", "message": "Password reset successfully."})
        except Exception as e: 
            return jsonify({"status": "error", "message": "Database error during reset."}), 500
        finally:
            conn.close()
    else: 
        return jsonify({"status": "error", "message": "Invalid or expired OTP."}), 400

@app.route('/api/users', methods=['GET'])
def get_users():
    conn = get_db_connection()
    try:
        users = conn.execute('SELECT * FROM users ORDER BY role, name').fetchall()
        return jsonify({"status": "success", "data": [dict(u) for u in users]}), 200
    except Exception as e: 
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/technicians', methods=['GET'])
def get_technicians():
    conn = get_db_connection()
    try:
        techs = conn.execute('SELECT * FROM technicians ORDER BY department, name').fetchall()
        return jsonify({"status": "success", "data": [dict(t) for t in techs]}), 200
    except Exception as e: 
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()
@app.route('/api/ai/quick-fix', methods=['POST'])
def ai_quick_fix():
    issue = request.json.get('issue')
    try:
        if client:
            prompt = f"A campus user is about to submit a maintenance ticket for this issue: '{issue}'. Give them a brief, friendly, 2-sentence DIY troubleshooting tip they can try right now to fix it themselves before calling a technician. Keep it safe and practical."
            response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
            return jsonify({"status": "success", "tip": response.text})
        return jsonify({"status": "error", "message": "AI not configured."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500        

@app.route('/api/tickets', methods=['POST'])
def create_ticket():
    data = request.json
    conn = get_db_connection()
    try:
        bldg = data.get('building')
        loc = data.get('location')

        # ==========================================
        # 1. STANDARD AI CLASSIFICATION & ROUTING
        # ==========================================
        # We MUST run the AI first so we know which department this ticket belongs to!
        ai_decision = classify_ticket_with_ai(data['issue'])
        ticket_dept = ai_decision['department']

        # ==========================================
        # 2. SMART DUPLICATION CHECK (DEPARTMENT SPECIFIC)
        # ==========================================
        # Allows a Plumbing and Electrical ticket in the same room, but blocks TWO identical department tickets.
        duplicate_check = conn.execute('''
            SELECT ticket_id, status FROM tickets
            WHERE building = ? AND location = ? AND department = ? AND status NOT IN ('Resolved', 'Closed', 'Cancelled')
        ''', (bldg, loc, ticket_dept)).fetchone()

        if duplicate_check:
            return jsonify({
                "status": "error",
                "message": f"Duplicate Prevented: Our {ticket_dept} team is already working on an active issue in {bldg} - {loc}!"
            }), 400
        
        conn.execute('INSERT INTO tickets (ticket_id, user_name, role, department, building, location, issue, priority, ai_analysis) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', 
            (data['ticket_id'], data['user_name'], data['role'], ai_decision['department'], data['building'], data['location'], data['issue'], ai_decision['priority'], ai_decision['ai_analysis']))
        
        add_notification(data['user_name'], None, f"Request {data['ticket_id']} successfully sent. AI is analyzing your issue.", db_conn=conn)
        
        # Route to Tech
        tech = tool_get_available_technician(ai_decision['department'], db_conn=conn)
        
        if tech['status'] == 'success':
            tool_assign_ticket(data['ticket_id'], tech['technician_name'], db_conn=conn)
            add_notification(tech['technician_name'], None, f"AI DISPATCH: {data['ticket_id']} routed to your tracking portal.", is_urgent=1 if ai_decision['priority'] == 'Urgent' else 0, db_conn=conn)
            add_notification(data['user_name'], None, f"Update: Ticket {data['ticket_id']} routed to Technician {tech['technician_name']}.", db_conn=conn)
        else:
            add_notification(None, 'Portal Admin', f"WARNING: {data['ticket_id']} could not be assigned. No techs in {ai_decision['department']}!", is_urgent=1, db_conn=conn)
            
        conn.commit() 
        return jsonify({"status": "success"}), 201
        
    except Exception as e: 
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/tickets', methods=['GET'])
def get_tickets():
    conn = get_db_connection()
    try:
        role = request.args.get('role')
        user_name = request.args.get('name')
        
        if role == 'Portal Admin':
            tickets = conn.execute("SELECT * FROM tickets WHERE status != 'Cancelled' ORDER BY created_at DESC").fetchall()
        elif role == 'Campus Technician':
            tickets = conn.execute("SELECT * FROM tickets WHERE assigned_technician = ? AND status != 'Cancelled' ORDER BY created_at DESC", (user_name,)).fetchall()
        else: 
            tickets = conn.execute("SELECT * FROM tickets WHERE user_name = ? AND status != 'Cancelled' ORDER BY created_at DESC", (user_name,)).fetchall()
            
        return jsonify({"status": "success", "data": [dict(t) for t in tickets]}), 200
    except Exception as e: 
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/tickets/<ticket_id>', methods=['DELETE'])
def delete_ticket(ticket_id):
    conn = get_db_connection()
    try:
        conn.execute("UPDATE tickets SET status = 'Cancelled' WHERE ticket_id = ?", (ticket_id,))
        log_audit(request.args.get('name', 'Admin'), 'DELETED_TICKET', ticket_id, db_conn=conn)
        conn.commit()
        return jsonify({"status": "success"}), 200
    except Exception as e: 
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()

# ↓↓↓ FIX: ACCEPT BUTTON NOW OFFICIALLY STARTS THE CLOCK ↓↓↓
@app.route('/api/tickets/<ticket_id>/accept', methods=['PUT'])
def accept(ticket_id):
    conn = get_db_connection()
    try:
        ticket = conn.execute('SELECT * FROM tickets WHERE ticket_id = ?', (ticket_id,)).fetchone()
        # The Fix: We inject started_at = CURRENT_TIMESTAMP right here!
        conn.execute("UPDATE tickets SET status = 'In Progress', started_at = CURRENT_TIMESTAMP WHERE ticket_id = ?", (ticket_id,))
        if ticket: 
            add_notification(ticket['user_name'], None, f"IN PROGRESS: Technician has started working on {ticket_id}.", db_conn=conn)
            notify_status_change(ticket_id, 'In Progress', db_conn=conn)
        conn.commit()
        return jsonify({"status": "success"}), 200
    except Exception as e: 
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()
# ↑↑↑ END OF FIX ↑↑↑

# ↓↓↓ REPLACED RESOLVE ROUTE & NEW START ROUTE ↓↓↓
@app.route('/api/tickets/<ticket_id>/start', methods=['PUT'])
def start_ticket(ticket_id):
    conn = get_db_connection()
    try:
        # Stamp the exact start time in UTC
        conn.execute("UPDATE tickets SET status = 'In Progress', started_at = CURRENT_TIMESTAMP, last_updated_at = CURRENT_TIMESTAMP WHERE ticket_id = ?", (ticket_id,))
        conn.commit()
        return jsonify({"status": "success"}), 200
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500
    finally: conn.close()

# ↓↓↓ FIX: BULLETPROOF SQLITE MATH SAFETY NET ↓↓↓
@app.route('/api/tickets/<ticket_id>/resolve', methods=['PUT'])
def resolve_ticket(ticket_id):
    conn = get_db_connection()
    try:
        # The Fix: COALESCE forces it to fall back to the created_at time if started_at is ever missing, preventing "None" crashes!
        conn.execute('''
            UPDATE tickets 
            SET status = 'Resolved', 
                time_taken_mins = COALESCE(CAST(MAX(1, (julianday(CURRENT_TIMESTAMP) - julianday(COALESCE(started_at, created_at))) * 1440) AS INTEGER), 1),
                last_updated_at = CURRENT_TIMESTAMP 
            WHERE ticket_id = ?
        ''', (ticket_id,))
# ↑↑↑ END OF FIX ↑↑↑
        
        # Notify user
        ticket = conn.execute("SELECT user_name, time_taken_mins FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if ticket:
            add_notification(ticket['user_name'], None, f"Your request {ticket_id} has been resolved! It took our team {ticket['time_taken_mins']} minutes to fix.", db_conn=conn)
            
        conn.commit()
        return jsonify({"status": "success"}), 200
    except Exception as e: 
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()
# ↑↑↑ END OF UPGRADED ROUTES ↑↑↑

@app.route('/api/tickets/<ticket_id>/request-part', methods=['POST'])
def request_part(ticket_id):
    conn = get_db_connection()
    try:
        data = request.json
        part_name = data.get('part_name')
        tech_name = data.get('tech_name')
        
        # 1. Update ticket to show it is paused waiting for inventory
        conn.execute("UPDATE tickets SET status = 'Awaiting Parts', last_updated_at = CURRENT_TIMESTAMP WHERE ticket_id = ?", (ticket_id,))
        conn.execute("INSERT INTO part_requests (ticket_id, tech_name, part_name) VALUES (?, ?, ?)", (ticket_id, tech_name, part_name))
        
        # 2. Ping the Admin and the Tech
        add_notification(None, 'Portal Admin', f"📦 INVENTORY ALERT: {tech_name} requested '{part_name}' for ticket {ticket_id}.", is_urgent=1, db_conn=conn)
        add_notification(tech_name, None, f"Part Requested: '{part_name}'. Admin has been notified.", db_conn=conn)
        
        conn.commit()
        return jsonify({"status": "success"}), 200
    except Exception as e: 
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/tickets/<ticket_id>/decline', methods=['PUT'])
def decline_ticket(ticket_id):
    conn = get_db_connection()
    try:
        data = request.json
        reason = data.get('reason', 'No reason provided')
        ticket = conn.execute('SELECT * FROM tickets WHERE ticket_id = ?', (ticket_id,)).fetchone()
        conn.execute("UPDATE tickets SET status = 'Decline Requested', decline_reason = ? WHERE ticket_id = ?", (reason, ticket_id))
        if ticket:
            add_notification(None, 'Portal Admin', f"ACTION REQUIRED: Tech {ticket['assigned_technician']} requested to decline {ticket_id}.", is_urgent=1, db_conn=conn)
            add_notification(ticket['user_name'], None, f"Update: Processing reassignment for {ticket_id} to ensure fastest resolution.", db_conn=conn)
        conn.commit()
        return jsonify({"status": "success"}), 200
    except Exception as e: 
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/tickets/<ticket_id>/approve_decline', methods=['PUT'])
def approve_decline(ticket_id):
    conn = get_db_connection()
    try:
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
                add_notification(old_tech, None, f"Admin Approved: You have been removed from ticket {ticket_id}.", db_conn=conn)
                add_notification(new_tech_name, None, f"REASSIGNED TASK: {ticket_id} has been added to your queue.", db_conn=conn)
                add_notification(ticket['user_name'], None, f"Update: {ticket_id} reassigned to {new_tech_name}.", db_conn=conn)
            else:
                conn.execute("UPDATE tickets SET assigned_technician = 'Unassigned', status = 'Pending', decline_reason = NULL WHERE ticket_id = ?", (ticket_id,))
                add_notification(None, 'Portal Admin', f"Unassigned: {ticket_id} returned to queue. No tech available.", is_urgent=1, db_conn=conn)
            conn.commit()
        return jsonify({"status": "success"}), 200
    except Exception as e: 
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/tickets/<ticket_id>/reject_decline', methods=['PUT'])
def reject_decline(ticket_id):
    conn = get_db_connection()
    try:
        ticket = conn.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
        conn.execute("UPDATE tickets SET status = 'Assigned', decline_reason = NULL WHERE ticket_id = ?", (ticket_id,))
        if ticket: 
            add_notification(ticket['assigned_technician'], None, f"ADMIN DENIED DECLINE: You are required to complete {ticket_id}.", is_urgent=1, db_conn=conn)
        conn.commit()
        return jsonify({"status": "success"}), 200
    except Exception as e: 
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/notifications', methods=['GET'])
def get_notifications():
    conn = get_db_connection()
    try:
        role = request.args.get('role')
        name = request.args.get('name')
        if role == 'Portal Admin': 
            notifs = conn.execute("SELECT * FROM system_notifications WHERE target_user = ? OR target_role = 'Portal Admin' ORDER BY id DESC LIMIT 50", (name,)).fetchall()
        else: 
            notifs = conn.execute("SELECT * FROM system_notifications WHERE target_user = ? ORDER BY id DESC LIMIT 50", (name,)).fetchall()
        return jsonify({"status": "success", "data": [dict(n) for n in notifs]}), 200
    except Exception as e: 
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/notifications/read', methods=['POST'])
def mark_read():
    conn = get_db_connection()
    try:
        data = request.json
        conn.execute("UPDATE system_notifications SET is_read = 1 WHERE target_user = ? OR target_role = ?", (data['name'], data['role']))
        conn.commit()
        return jsonify({"status": "success"})
    except Exception as e: 
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()


# ↓↓↓ PASTE THE NEW QA ENDPOINT EXACTLY HERE ↓↓↓
@app.route('/api/leaderboard', methods=['GET'])
def get_leaderboard():
    conn = get_db_connection()
    try:
        # Fetch the top 5 technicians ordered by their points
        leaders = conn.execute("SELECT name, department, points FROM technicians WHERE points > 0 ORDER BY points DESC LIMIT 5").fetchall()
        return jsonify({"status": "success", "data": [dict(l) for l in leaders]}), 200
    except Exception as e: 
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()





@app.route('/api/qa/<ticket_id>', methods=['GET'])
def handle_qa_response(ticket_id):
    answer = request.args.get('answer', 'yes').lower()
    conn = get_db_connection()
    try:
        ticket = conn.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if not ticket:
            return "<h1>Error</h1><p>Ticket not found.</p>", 404
            
        if answer == 'yes':
            # Job well done. Close it out permanently.
            conn.execute("UPDATE tickets SET status = 'Closed', last_updated_at = CURRENT_TIMESTAMP WHERE ticket_id = ?", (ticket_id,))
            conn.execute("UPDATE technicians SET points = points + 10 WHERE name = ?", (ticket['assigned_technician'],)) # <--- ADD THIS LINE
            add_notification(ticket['assigned_technician'], None, f"🎉 QA Passed! +10 Points awarded. User confirmed {ticket_id} is fixed.", db_conn=conn)
            conn.commit()
            return "<h1 style='color: #10b981; font-family: sans-serif;'>Thank you!</h1><p style='font-family: sans-serif;'>We are glad the issue is resolved. Have a great day!</p>"
        
        else:
            # AI ESCALATION: The tech failed the QA check. PENALIZE 5 POINTS!
            conn.execute("UPDATE tickets SET status = 'Escalated', priority = 'Urgent', last_updated_at = CURRENT_TIMESTAMP WHERE ticket_id = ?", (ticket_id,))
            conn.execute("UPDATE technicians SET points = points - 5 WHERE name = ?", (ticket['assigned_technician'],)) # <--- ADD THIS LINE
            add_notification(None, 'Portal Admin', f"🚨 QA FAILED: {ticket_id} was not fixed properly by {ticket['assigned_technician']}! Escalated to URGENT.", is_urgent=1, db_conn=conn)
            add_notification(ticket['assigned_technician'], None, f"⚠️ QA ALERT: -5 Points. User reported {ticket_id} is STILL BROKEN.", is_urgent=1, db_conn=conn)
            conn.commit()
            return "<h1 style='color: #ef4444; font-family: sans-serif;'>Apologies!</h1><p style='font-family: sans-serif;'>The ticket has been automatically reopened, escalated to URGENT priority, and the Admin has been notified.</p>"
    except Exception as e:
        return str(e), 500
    finally:
        conn.close()
@app.route('/api/technicians/toggle-shift', methods=['PUT'])
def toggle_shift():
    data = request.json
    tech_name = data.get('name')
    conn = get_db_connection()
    try:
        tech = conn.execute("SELECT is_on_shift FROM technicians WHERE name = ?", (tech_name,)).fetchone()
        if not tech:
            return jsonify({"status": "error", "message": "Technician not found."}), 404
            
        new_status = 0 if tech['is_on_shift'] == 1 else 1
        conn.execute("UPDATE technicians SET is_on_shift = ? WHERE name = ?", (new_status, tech_name))
        
        status_msg = "Clocked In: You are now receiving AI dispatches." if new_status == 1 else "Clocked Out: AI dispatches paused."
        add_notification(tech_name, None, status_msg, db_conn=conn)
        
        conn.commit()
        return jsonify({"status": "success", "is_on_shift": new_status}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/inventory', methods=['GET'])
def get_inventory():
    conn = get_db_connection()
    try:
        items = conn.execute("SELECT * FROM inventory ORDER BY stock_level ASC").fetchall()
        requests = conn.execute("SELECT * FROM part_requests ORDER BY requested_at DESC").fetchall()
        return jsonify({"status": "success", "inventory": [dict(i) for i in items], "requests": [dict(r) for r in requests]}), 200
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500
    finally: conn.close()

# ↓↓↓ REPLACED ROUTE WITH DEDUCTION & NEW AI ROUTES ↓↓↓
@app.route('/api/inventory/request-action', methods=['POST'])
def handle_part_request():
    data = request.json
    req_id = data.get('request_id')
    action = data.get('action') 
    conn = get_db_connection()
    try:
        part_req = conn.execute("SELECT * FROM part_requests WHERE id = ?", (req_id,)).fetchone()
        if not part_req: return jsonify({"status": "error", "message": "Request not found"}), 404
        
        if action == 'Approve':
            conn.execute("UPDATE part_requests SET status = 'Approved' WHERE id = ?", (req_id,))
            conn.execute("UPDATE tickets SET status = 'In Progress', last_updated_at = CURRENT_TIMESTAMP WHERE ticket_id = ?", (part_req['ticket_id'],))
            
            # THE FIX: Deduct the stock! Uses 'LIKE' to match the name even if spelled slightly differently.
            conn.execute("UPDATE inventory SET stock_level = MAX(0, stock_level - 1) WHERE LOWER(item_name) LIKE ?", (f"%{part_req['part_name'].lower()}%",))
            
            add_notification(part_req['tech_name'], None, f"✅ APPROVED: Part '{part_req['part_name']}' is ready for pickup. Ticket {part_req['ticket_id']} resumed.", db_conn=conn)
        else:
            conn.execute("UPDATE part_requests SET status = 'Denied' WHERE id = ?", (req_id,))
            conn.execute("UPDATE tickets SET status = 'In Progress', last_updated_at = CURRENT_TIMESTAMP WHERE ticket_id = ?", (part_req['ticket_id'],))
            add_notification(part_req['tech_name'], None, f"❌ DENIED: Request for '{part_req['part_name']}' was rejected.", is_urgent=1, db_conn=conn)
            
        conn.commit()
        return jsonify({"status": "success"}), 200
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500
    finally: conn.close()

@app.route('/api/inventory/search-online', methods=['POST'])
def search_online():
    part_name = request.json.get('part_name')
    try:
        if client: 
            prompt = f"Search the live internet for the current price of '{part_name}' in India. Provide a strictly factual 3-sentence summary: State the exact real website name where it is currently sold (e.g., Amazon.in, Moglix, IndustryBuying, or IndiaMart), the actual real-time price in INR (₹), and the estimated delivery time. Do not make up prices; use the live search data."
            
            # The Magic: This tells Gemini to actually browse Google Search!
            config = types.GenerateContentConfig(
                tools=[{"google_search": {}}]
            )
            response = client.models.generate_content(
                model='gemini-2.5-flash', 
                contents=prompt,
                config=config
            )
            analysis = response.text
        else:
            analysis = f"Estimated price for {part_name} is ₹850 on Amazon Business India. Delivery: 2 Days."
        return jsonify({"status": "success", "analysis": analysis})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/inventory/buy', methods=['POST'])
def buy_part():
    part_name = request.json.get('part_name')
    conn = get_db_connection()
    try:
        conn.execute("UPDATE inventory SET stock_level = stock_level + 10 WHERE item_name = ?", (part_name,))
        add_notification(None, 'Portal Admin', f"📦 PROCUREMENT: 10 units of '{part_name}' ordered and added to local stock.", db_conn=conn)
        conn.commit()
        return jsonify({"status": "success"})
    finally:
        conn.close()
# ↑↑↑ END OF UPGRADED BLOCK ↑↑↑


# ==========================================
# 6. CENTRAL AI MONITORING LOOP
# ==========================================
# ==========================================
# 6. CENTRAL AI MONITORING LOOP
# ==========================================
def monitoring_agent_loop():
    time.sleep(2)
    last_reset_date = None
    last_pm_time = time.time() # Track time for Preventive Maintenance
    
    while True:
        conn = None
        try:
            conn = get_db_connection() 
            now = datetime.now()
            current_date = now.date()
            
            # ==========================================
            # 1. PREVENTIVE MAINTENANCE ENGINE (Runs every 2 mins for testing)
            # ==========================================
            if time.time() - last_pm_time > 7776000:  # 7,776,000 seconds = ~3 months
                try:
                    # Pick a random routine task to generate
                    task = random.choice([
                        {"issue": "Routine Check: Inspect Building A HVAC Filters", "dept": "Air Conditioning & Ventilation Services", "bldg": "Building A", "loc": "Roof"},
                        {"issue": "Routine Check: Test Fire Alarms & Extinguishers", "dept": "Security & Surveillance", "bldg": "Main Building", "loc": "All Floors"},
                        {"issue": "Routine Check: Main Server UPS Battery Diagnostic", "dept": "IT & Network Services", "bldg": "Building C", "loc": "Server Room 1"}
                    ])
                    
                    pm_ticket_id = 'PM-' + str(random.randint(1000, 9999))
                    
                    conn.execute('''INSERT INTO tickets 
                        (ticket_id, user_name, role, department, building, location, issue, priority, ai_analysis) 
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''', 
                        (pm_ticket_id, 'System AI', 'AI Engine', task['dept'], task['bldg'], task['loc'], task['issue'], 'Low', 'PREVENTIVE MAINTENANCE: Auto-generated scheduled task.'))
                    
                    # Try to assign it immediately
                    tech = tool_get_available_technician(task['dept'], db_conn=conn)
                    if tech['status'] == 'success':
                        tool_assign_ticket(pm_ticket_id, tech['technician_name'], db_conn=conn)
                        add_notification(tech['technician_name'], None, f"PREVENTIVE TASK: {pm_ticket_id} automatically assigned to your queue.", db_conn=conn)
                    
                    print(f"⚙️ [PREVENTIVE ENGINE] Auto-generated routine maintenance task: {pm_ticket_id}")
                    last_pm_time = time.time() # Reset the timer
                except Exception as e:
                    print("PM Engine Error:", e)

            # ==========================================
            # 2. AI QA AGENT (Sends 1-minute post-resolution survey)
            # ==========================================
            # ... (KEEP YOUR EXISTING QA AGENT CODE HERE) ...
            qa_pending = conn.execute("""
                SELECT ticket_id, user_name, assigned_technician FROM tickets 
                WHERE status = 'Resolved' AND qa_sent = 0 
                AND datetime(last_updated_at) <= datetime('now', '-24 hours')
            """).fetchall()
            
            for q in qa_pending:
                ticket_id = q['ticket_id']
                user = conn.execute("SELECT email FROM users WHERE name = ?", (q['user_name'],)).fetchone()
                
                if user and user['email']:
                    subject = f"Did we fix it? QA Survey for {ticket_id}"
                    link_yes = f"http://127.0.0.1:5005/api/qa/{ticket_id}?answer=yes"
                    link_no = f"http://127.0.0.1:5005/api/qa/{ticket_id}?answer=no"
                    
                    body = f"Hi {q['user_name']},\n\nYour maintenance request {ticket_id} was marked as 'Resolved' by {q['assigned_technician']}.\n\nTo ensure quality, please let us know if the issue was completely fixed by clicking one of the links below:\n\nYES, IT IS FIXED: {link_yes}\n\nNO, STILL BROKEN: {link_no}\n\nThank you,\nInfraHub QA Bot"
                    
                    threading.Thread(target=send_async_email, args=(app, user['email'], subject, body)).start()
                    print(f"🤖 [QA AGENT] Sent quality check email to {q['user_name']} for {ticket_id}")
                
                # Mark as sent so we don't spam them
                conn.execute("UPDATE tickets SET qa_sent = 1 WHERE ticket_id = ?", (ticket_id,))
            
            
            
            # ↓↓↓ REPLACED AI DISPATCHER: DYNAMIC MINUTE TRACKING ↓↓↓
            techs = conn.execute("SELECT name, max_shift_hours FROM technicians WHERE is_on_shift = 1").fetchall()
            for t in techs:
                max_minutes = (t['max_shift_hours'] if t['max_shift_hours'] else 8) * 60
                
                # 1. Calculate EXACT minutes worked today based on Resolved/Closed tickets
                minutes_worked_today = conn.execute('''
                    SELECT SUM(time_taken_mins) as total_mins FROM tickets 
                    WHERE assigned_technician = ? 
                    AND status IN ('Resolved', 'Closed') 
                    AND date(last_updated_at) = date('now', 'localtime')
                ''', (t['name'],)).fetchone()['total_mins'] or 0
                
                # 2. Check how many tasks they are currently holding
                active_tickets = conn.execute('''
                    SELECT ticket_id FROM tickets 
                    WHERE assigned_technician = ? AND status IN ('Assigned', 'In Progress', 'Awaiting Parts') 
                    ORDER BY created_at ASC
                ''', (t['name'],)).fetchall()
                
                # 3. Prevent Overloading: Only allow 1 active task at a time!
                if len(active_tickets) > 1:
                    excess_count = len(active_tickets) - 1
                    for excess in active_tickets[1:]:
                        conn.execute("UPDATE tickets SET status = 'Pending' WHERE ticket_id = ?", (excess['ticket_id'],))
                    active_tickets = active_tickets[:1]
                
                # 4. Update UI to show exact minutes worked today vs total shift minutes
                conn.execute("UPDATE technicians SET current_active_hours = ? WHERE name = ?", (minutes_worked_today, t['name']))
                
                # 5. ASSIGNMENT LOGIC: If they have time left in their shift AND no active tasks, give them 1 task
                if minutes_worked_today < max_minutes and len(active_tickets) == 0:
                    next_ticket = conn.execute('''
                        SELECT ticket_id FROM tickets 
                        WHERE assigned_technician = ? AND status = 'Pending' 
                        ORDER BY priority DESC, created_at ASC LIMIT 1
                    ''', (t['name'],)).fetchone()
                    
                    if next_ticket:
                        conn.execute("UPDATE tickets SET status = 'Assigned' WHERE ticket_id = ?", (next_ticket['ticket_id'],))
                        add_notification(t['name'], None, f"NEW ASSIGNMENT: Task {next_ticket['ticket_id']} assigned.", db_conn=conn)
# ↑↑↑ END OF REPLACED AI DISPATCHER ↑↑↑
                
            overdue = conn.execute("SELECT ticket_id FROM tickets WHERE priority IN ('Urgent', 'High') AND status = 'Pending' AND assigned_technician = 'Unassigned'").fetchall()
            for o in overdue:
                conn.execute("UPDATE tickets SET status = 'Escalated' WHERE ticket_id = ?", (o['ticket_id'],))
                add_notification(None, 'Portal Admin', f"ESCALATION: {o['ticket_id']} is critical and unassigned! Intervene.", is_urgent=1, db_conn=conn)
            
            unassigned = conn.execute("SELECT ticket_id, department FROM tickets WHERE assigned_technician = 'Unassigned' AND status = 'Pending'").fetchall()
            for u in unassigned:
                tech = tool_get_available_technician(u['department'], db_conn=conn)
                if tech['status'] == 'success':
                    tool_assign_ticket(u['ticket_id'], tech['technician_name'], db_conn=conn)
                    
            conn.commit()
        except Exception as e: 
            print("Monitor Loop Error:", e)
        finally:
            if conn:
                conn.close() 
        
        time.sleep(5)

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')
@app.route('/api/debug/audit')
def check_audit():
    conn = get_db_connection()
    try:
        logs = conn.execute("SELECT * FROM audit_logs ORDER BY created_at DESC").fetchall()
        return jsonify([dict(l) for l in logs])
    finally:
        conn.close()

if __name__ == '__main__':
    init_db()
    monitor_thread = threading.Thread(target=monitoring_agent_loop, daemon=True)
    monitor_thread.start()
    app.run(debug=True, port=5005, use_reloader=False)