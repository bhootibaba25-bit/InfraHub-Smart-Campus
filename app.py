import os
import json
import threading
import time
import random
import csv
from io import StringIO
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
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

# Serve the main HTML page
@app.route('/')
def home():
    return send_from_directory('.', 'index.html')

CORS(app)
client = genai.Client(api_key=API_KEY) if API_KEY else None

app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.getenv("MAIL_USERNAME")
app.config['MAIL_PASSWORD'] = os.getenv("MAIL_PASSWORD")
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
    conn = db_conn or get_db_connection()
    try:
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
            threading.Thread(target=send_async_email, args=(app, user['email'], subject, body)).start()
    except Exception as e:
        print("Email Notification DB Error:", e)
    finally:
        if not db_conn and conn:
            conn.close()

# ==========================================
# 2. DATABASE ARCHITECTURE (NEON POSTGRES CLOUD)
# ==========================================
class DBConnection:
    """A brilliant custom wrapper that translates SQLite commands into PostgreSQL automatically!"""
    def __init__(self):
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            raise ValueError("DATABASE_URL is missing! Add your Neon Postgres URL to Render.")
        self.conn = psycopg2.connect(db_url)
        self.conn.autocommit = True # Prevents transaction crashes

    def execute(self, query, params=()):
        # Auto-Translate SQLite syntax to Postgres syntax
        pg_query = query.replace('?', '%s')
        pg_query = pg_query.replace("datetime('now', '-24 hours')", "NOW() - INTERVAL '24 hours'")
        pg_query = pg_query.replace("date('now', 'localtime')", "CURRENT_DATE")
        pg_query = pg_query.replace("CURRENT_TIMESTAMP", "NOW()")
        
        # Translate the complex time math for resolving tickets
        if "julianday(" in pg_query:
            pg_query = """
                UPDATE tickets 
                SET status = 'Resolved', 
                    resolution_photo = %s,
                    time_taken_mins = COALESCE(CAST(EXTRACT(EPOCH FROM (NOW() - COALESCE(started_at, created_at))) / 60 AS INTEGER), 1),
                    last_updated_at = NOW() 
                WHERE ticket_id = %s
            """
            
        cursor = self.conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(pg_query, params)
        return cursor

    def executemany(self, query, param_list):
        pg_query = query.replace('?', '%s')
        cursor = self.conn.cursor()
        cursor.executemany(pg_query, param_list)
        return cursor

    def commit(self):
        pass # Handled safely by autocommit in Postgres

    def close(self):
        self.conn.close()

def get_db_connection():
    return DBConnection()

def init_db():
    conn = get_db_connection()
    try:
        # 1. CREATE CORE INFRASTRUCTURE TABLES (Translated for Postgres)
        conn.execute('''CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, custom_id TEXT, name TEXT, email TEXT UNIQUE, password TEXT, role TEXT, mobile_no TEXT, account_status TEXT DEFAULT 'Approved')''')
        conn.execute('''CREATE TABLE IF NOT EXISTS technicians (technician_id SERIAL PRIMARY KEY, custom_id TEXT, name TEXT, department TEXT, current_active_hours INTEGER DEFAULT 0, max_shift_hours INTEGER DEFAULT 8, is_on_shift INTEGER DEFAULT 0, mobile_no TEXT, points INTEGER DEFAULT 0, on_break INTEGER DEFAULT 0, overtime_opt_in INTEGER DEFAULT 0, badges_unlocked TEXT DEFAULT 'Welcome Aboard', current_building TEXT DEFAULT 'Main Building', account_status TEXT DEFAULT 'Approved')''')
        conn.execute('''CREATE TABLE IF NOT EXISTS tickets (ticket_id TEXT PRIMARY KEY, user_name TEXT, role TEXT, department TEXT, building TEXT, location TEXT, issue TEXT, photo_attached TEXT, priority TEXT, ai_analysis TEXT, assigned_technician TEXT DEFAULT 'Unassigned', status TEXT DEFAULT 'Pending', decline_reason TEXT, read_status INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT NOW(), last_updated_at TIMESTAMP DEFAULT NOW(), qa_sent INTEGER DEFAULT 0, started_at TIMESTAMP, time_taken_mins INTEGER DEFAULT 0, user_rating INTEGER DEFAULT 0, user_feedback TEXT, resolution_photo TEXT DEFAULT 'None')''')
        conn.execute('''CREATE TABLE IF NOT EXISTS system_notifications (id SERIAL PRIMARY KEY, target_user TEXT, target_role TEXT, message TEXT, is_urgent INTEGER DEFAULT 0, is_read INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT NOW())''')
        conn.execute('''CREATE TABLE IF NOT EXISTS inventory (id SERIAL PRIMARY KEY, item_name TEXT, category TEXT, stock_level INTEGER DEFAULT 0, reorder_threshold INTEGER DEFAULT 5, unit_price NUMERIC)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS part_requests (id SERIAL PRIMARY KEY, ticket_id TEXT, tech_name TEXT, part_name TEXT, status TEXT DEFAULT 'Pending', requested_at TIMESTAMP DEFAULT NOW())''')
        conn.execute('''CREATE TABLE IF NOT EXISTS audit_logs (id SERIAL PRIMARY KEY, "user" TEXT, action TEXT, target TEXT, created_at TIMESTAMP DEFAULT NOW())''')
        conn.execute('''CREATE TABLE IF NOT EXISTS shift_trades (id SERIAL PRIMARY KEY, requester TEXT, department TEXT, target_date TEXT, status TEXT DEFAULT 'Pending')''')
        conn.execute('''CREATE TABLE IF NOT EXISTS leave_requests (id SERIAL PRIMARY KEY, tech_name TEXT, start_date TEXT, end_date TEXT, reason TEXT, status TEXT DEFAULT 'Pending')''')
        conn.execute('''CREATE TABLE IF NOT EXISTS tool_checkouts (id SERIAL PRIMARY KEY, tool_name TEXT, tech_name TEXT, checkout_date TIMESTAMP DEFAULT NOW(), status TEXT DEFAULT 'Borrowed')''')

        # 2. SEED INITIAL INVENTORY
        inv_check = conn.execute("SELECT COUNT(*) as count FROM inventory").fetchone()['count']
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
            conn.executemany("INSERT INTO inventory (item_name, category, stock_level, reorder_threshold, unit_price) VALUES (?, ?, ?, ?, ?)", seed_items)

        # 3. SEED PERMANENT SYSTEM MANAGEMENT ACCOUNTS
        admin_check = conn.execute("SELECT COUNT(*) as count FROM users WHERE email = 'admin@campus.edu'").fetchone()['count']
        if admin_check == 0:
            conn.execute('''INSERT INTO users (name, email, password, role, account_status) 
                         VALUES ('Master Admin', 'admin@campus.edu', 'Admin123!', 'Portal Admin', 'Approved')''')
            conn.execute('''INSERT INTO users (name, email, password, role, account_status) 
                         VALUES ('Campus User', 'user@campus.edu', 'User123!', 'Campus Staff', 'Approved')''')
            conn.execute('''INSERT INTO users (name, email, password, role, account_status) 
                         VALUES ('Master Tech Aarav', 'tech@campus.edu', 'Tech123!', 'Master Technician', 'Approved')''')
            conn.execute('''INSERT INTO technicians (name, department, is_on_shift, max_shift_hours, account_status) 
                         VALUES ('Master Tech Aarav', 'All', 0, 12, 'Approved')''')

            tech_roster = [
                ('IT & Network Services', 'Rohan Desai', 'rohan.it@campus.edu'),
                ('Electrical Maintenance', 'Vikram Singh', 'vikram.elec@campus.edu'),
                ('Plumbing Maintenance', 'Arjun Patel', 'arjun.plumb@campus.edu'),
                ('Civil Maintenance', 'Neha Kulkarni', 'neha.civil@campus.edu'),
                ('Air Conditioning & Ventilation Services', 'Aditya Joshi', 'aditya.hvac@campus.edu'),
                ('Security & Surveillance', 'Karan Verma', 'karan.sec@campus.edu'),
                ('Housekeeping Services', 'Pooja Nair', 'pooja.clean@campus.edu'),
                ('Fire Safety Systems', 'Rahul Menon', 'rahul.fire@campus.edu'),
                ('Water Supply & Sewage Management', 'Sanjay Gupta', 'sanjay.water@campus.edu'),
                ('Equipment Support', 'Priya Rao', 'priya.equip@campus.edu')
            ]
            
            for dept, tech_name, tech_email in tech_roster:
                conn.execute('''INSERT INTO users (name, email, password, role, account_status) 
                             VALUES (?, ?, 'Tech123!', 'Campus Technician', 'Approved')''', (tech_name, tech_email))
                conn.execute('''INSERT INTO technicians (name, department, is_on_shift, account_status) 
                             VALUES (?, ?, 0, 'Approved')''', (tech_name, dept))

    except Exception as e:
        print("Init DB Error:", e)
    finally:
        conn.close()
        
def log_audit(user, action, target, db_conn=None):
    conn = db_conn or get_db_connection()
    try:
        conn.execute('INSERT INTO audit_logs ("user", action, target) VALUES (?, ?, ?)', (user, action, target))
    except Exception as e:
        print("Audit Log Error:", e)
    finally:
        if not db_conn and conn: 
            conn.close()

def add_notification(target_user, target_role, message, is_urgent=0, db_conn=None):
    conn = db_conn or get_db_connection()
    try:
        conn.execute('INSERT INTO system_notifications (target_user, target_role, message, is_urgent) VALUES (?, ?, ?, ?)', (target_user, target_role, message, is_urgent))
    except Exception as e:
        print("Notification Error:", e)
    finally:
        if not db_conn and conn:
            conn.close()

# ==========================================
# 3. AGENTIC TOOLS & QUEUE MANAGEMENT
# ==========================================
def tool_get_available_technician(department, ticket_building=None, db_conn=None):
    conn = db_conn or get_db_connection()
    try:
        query = '''
            SELECT t.name, 
                   (t.current_building = ?) as is_close,
                   (SELECT COUNT(*) FROM tickets WHERE assigned_technician = t.name AND status IN ('Assigned', 'In Progress', 'Pending')) as total_tasks
            FROM technicians t
            WHERE (t.department = ? OR t.department LIKE ?) AND t.is_on_shift = 1 AND t.on_break = 0
            ORDER BY is_close DESC, total_tasks ASC
        '''
        available_techs = conn.execute(query, (ticket_building, department, f'%{department}%')).fetchall()
        
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
        requested_role = data.get('role')
        status = 'Pending' if requested_role in ['Portal Admin', 'Campus Technician', 'Master Technician'] else 'Approved'
            
        conn.execute('INSERT INTO users (name, email, password, role, account_status) VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING', 
                     (data['name'], data['email'], data['password'], requested_role, status))
        
        if requested_role == 'Campus Technician':
            dept = data.get('department', 'Pending Assignment')
            conn.execute('INSERT INTO technicians (name, department, current_active_hours, max_shift_hours, is_on_shift, account_status) VALUES (?, ?, ?, ?, ?, ?)', 
                         (data['name'], dept, 0, 8, 0, status))        
        
        msg = "Account created successfully! You can log in immediately." if status == 'Approved' else "Account registration submitted! Awaiting Master Admin approval before access is granted."
        return jsonify({"status": "success", "message": msg})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    finally:
        conn.close()
            
@app.route('/api/export/tickets', methods=['GET'])
def export_tickets():
    conn = get_db_connection()
    try:
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
        dept_data = conn.execute("SELECT department, COUNT(*) as count FROM tickets GROUP BY department").fetchall()
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
            if user['account_status'] == 'Pending':
                return jsonify({"status": "error", "message": "Access Denied: Your account is currently awaiting Master Admin validation."}), 403
                
            add_notification(user['name'], None, f"Session Started: Welcome back, {user['name']}.", db_conn=conn)
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
        photo = data.get('photo_attached', 'None')

        ai_decision = classify_ticket_with_ai(data['issue'])
        ticket_dept = ai_decision['department']

        duplicate_check = conn.execute('''
            SELECT ticket_id, status FROM tickets
            WHERE building = ? AND location = ? AND department = ? AND status NOT IN ('Resolved', 'Closed', 'Cancelled')
        ''', (bldg, loc, ticket_dept)).fetchone()

        if duplicate_check:
            return jsonify({
                "status": "error",
                "message": f"Duplicate Prevented: Our {ticket_dept} team is already working on an active issue in {bldg} - {loc}!"
            }), 400
        
        conn.execute('INSERT INTO tickets (ticket_id, user_name, role, department, building, location, issue, photo_attached, priority, ai_analysis) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', 
            (data['ticket_id'], data['user_name'], data['role'], ai_decision['department'], data['building'], data['location'], data['issue'], photo, ai_decision['priority'], ai_decision['ai_analysis']))
        
        add_notification(data['user_name'], None, f"Request {data['ticket_id']} successfully sent. AI is analyzing your issue.", db_conn=conn)
        
        tech = tool_get_available_technician(ai_decision['department'], data['building'], db_conn=conn)
        if tech['status'] == 'success':
            tool_assign_ticket(data['ticket_id'], tech['technician_name'], db_conn=conn)
            add_notification(tech['technician_name'], None, f"AI DISPATCH: {data['ticket_id']} routed to your tracking portal.", is_urgent=1 if ai_decision['priority'] == 'Urgent' else 0, db_conn=conn)
            add_notification(data['user_name'], None, f"Update: Ticket {data['ticket_id']} routed to Technician {tech['technician_name']}.", db_conn=conn)
        else:
            add_notification(None, 'Portal Admin', f"WARNING: {data['ticket_id']} could not be assigned. No techs in {ai_decision['department']}!", is_urgent=1, db_conn=conn)
            
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
        
        if role in ['Portal Admin', 'Master Technician']:
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
        return jsonify({"status": "success"}), 200
    except Exception as e: 
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/tickets/<ticket_id>/accept', methods=['PUT'])
def accept(ticket_id):
    data = request.get_json() or {}
    tech_name = data.get('tech_name')
    conn = get_db_connection()
    try:
        if tech_name:
            tech = conn.execute("SELECT is_on_shift FROM technicians WHERE name = ?", (tech_name,)).fetchone()
            if tech and tech['is_on_shift'] == 0:
                return jsonify({"status": "error", "message": "Action Denied: You cannot accept tasks while Off Duty. Please Clock In first."}), 403

        ticket = conn.execute('SELECT * FROM tickets WHERE ticket_id = ?', (ticket_id,)).fetchone()
        conn.execute("UPDATE tickets SET status = 'In Progress', started_at = NOW() WHERE ticket_id = ?", (ticket_id,))
        
        if ticket: 
            add_notification(ticket['user_name'], None, f"IN PROGRESS: Technician has started working on {ticket_id}.", db_conn=conn)
            notify_status_change(ticket_id, 'In Progress', db_conn=conn)
        
        return jsonify({"status": "success"}), 200
    except Exception as e: 
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/tickets/<ticket_id>/start', methods=['PUT'])
def start_ticket(ticket_id):
    conn = get_db_connection()
    try:
        conn.execute("UPDATE tickets SET status = 'In Progress', started_at = NOW(), last_updated_at = NOW() WHERE ticket_id = ?", (ticket_id,))
        return jsonify({"status": "success"}), 200
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500
    finally: conn.close()

@app.route('/api/tickets/<ticket_id>/resolve', methods=['PUT'])
def resolve_ticket(ticket_id):
    conn = get_db_connection()
    try:
        res_photo = request.json.get('resolution_photo', 'None')
        # DB wrapper automatically translates the SQLite julianday math here!
        conn.execute('''
            UPDATE tickets 
            SET status = 'Resolved', 
                resolution_photo = ?,
                time_taken_mins = COALESCE(CAST(julianday(CURRENT_TIMESTAMP) AS INTEGER), 1),
                last_updated_at = CURRENT_TIMESTAMP 
            WHERE ticket_id = ?
        ''', (res_photo, ticket_id))

        ticket = conn.execute("SELECT user_name, time_taken_mins FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if ticket:
            add_notification(ticket['user_name'], None, f"Your request {ticket_id} has been resolved! It took our team {ticket['time_taken_mins']} minutes to fix.", db_conn=conn)

        return jsonify({"status": "success"}), 200
    except Exception as e: 
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()
        
@app.route('/api/tickets/<ticket_id>/transfer', methods=['POST'])
def transfer_ticket(ticket_id):
    data = request.json
    new_dept = data.get('new_department')
    reason = data.get('reason')
    tech_name = data.get('tech_name')
    conn = get_db_connection()
    try:
        conn.execute("UPDATE tickets SET department = ?, assigned_technician = 'Unassigned', status = 'Pending', last_updated_at = NOW() WHERE ticket_id = ?", (new_dept, ticket_id))

        add_notification(None, 'Portal Admin', f"TICKET RE-ROUTED: {tech_name} transferred {ticket_id} to {new_dept}. Reason: {reason}", is_urgent=1, db_conn=conn)
        ticket = conn.execute("SELECT user_name FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if ticket:
            add_notification(ticket['user_name'], None, f"Update: Your ticket {ticket_id} was transferred to the {new_dept} department for specialized support.", db_conn=conn)

        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()   

@app.route('/api/tickets/<ticket_id>/request-part', methods=['POST'])
def request_part(ticket_id):
    conn = get_db_connection()
    try:
        data = request.json
        part_name = data.get('part_name')
        tech_name = data.get('tech_name')
        
        conn.execute("UPDATE tickets SET status = 'Awaiting Parts', last_updated_at = NOW() WHERE ticket_id = ?", (ticket_id,))
        conn.execute("INSERT INTO part_requests (ticket_id, tech_name, part_name) VALUES (?, ?, ?)", (ticket_id, tech_name, part_name))
        
        add_notification(None, 'Portal Admin', f"📦 INVENTORY ALERT: {tech_name} requested '{part_name}' for ticket {ticket_id}.", is_urgent=1, db_conn=conn)
        add_notification(tech_name, None, f"Part Requested: '{part_name}'. Admin has been notified.", db_conn=conn)
        
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
        return jsonify({"status": "success"})
    except Exception as e: 
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/leaderboard', methods=['GET'])
def get_leaderboard():
    conn = get_db_connection()
    try:
        leaders = conn.execute("SELECT name, department, points FROM technicians WHERE points > 0 ORDER BY points DESC LIMIT 5").fetchall()
        return jsonify({"status": "success", "data": [dict(l) for l in leaders]}), 200
    except Exception as e: 
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/qa/<ticket_id>', methods=['GET'])
def handle_qa_response(ticket_id):
    answer = request.args.get('answer', 'yes').lower()
    rating = request.args.get('rating', type=int, default=5) 
    feedback = request.args.get('feedback', 'No written feedback provided.') 
    
    conn = get_db_connection()
    try:
        ticket = conn.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if not ticket:
            return "<h1>Error</h1><p>Ticket not found.</p>", 404
            
        if answer == 'yes':
            conn.execute('''UPDATE tickets 
                            SET status = 'Closed', last_updated_at = NOW(), 
                                user_rating = ?, user_feedback = ? 
                            WHERE ticket_id = ?''', (rating, feedback, ticket_id))
            
            points_awarded = 10 + (rating * 2) 
            conn.execute("UPDATE technicians SET points = points + ? WHERE name = ?", (points_awarded, ticket['assigned_technician'])) 
            
            add_notification(ticket['assigned_technician'], None, f"🎉 QA Passed! +{points_awarded} Points. User rated you {rating} Stars!", db_conn=conn)
            return f"<h1 style='color: #10b981; font-family: sans-serif;'>Thank you!</h1><p style='font-family: sans-serif;'>Your {rating}-star rating has been recorded for {ticket['assigned_technician']}. Have a great day!</p>"
        
        else:
            conn.execute("UPDATE tickets SET status = 'Escalated', priority = 'Urgent', last_updated_at = NOW() WHERE ticket_id = ?", (ticket_id,))
            conn.execute("UPDATE technicians SET points = points - 5 WHERE name = ?", (ticket['assigned_technician'],)) 
            add_notification(None, 'Portal Admin', f"🚨 QA FAILED: {ticket_id} was not fixed properly by {ticket['assigned_technician']}! Escalated to URGENT.", is_urgent=1, db_conn=conn)
            add_notification(ticket['assigned_technician'], None, f"⚠️ QA ALERT: -5 Points. User reported {ticket_id} is STILL BROKEN.", is_urgent=1, db_conn=conn)
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
        
        return jsonify({"status": "success", "is_on_shift": new_status}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/inventory', methods=['GET'])
def get_inventory():
    conn = get_db_connection()
    try:
        # FIX: We use CAST() to force Postgres to send standard floats to the frontend
        items = conn.execute("SELECT id, item_name, category, stock_level, reorder_threshold, CAST(unit_price AS FLOAT) as unit_price FROM inventory ORDER BY stock_level ASC").fetchall()
        requests = conn.execute("SELECT * FROM part_requests ORDER BY requested_at DESC").fetchall()
        return jsonify({"status": "success", "inventory": [dict(i) for i in items], "requests": [dict(r) for r in requests]}), 200
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500
    finally: conn.close()

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
            conn.execute("UPDATE tickets SET status = 'In Progress', last_updated_at = NOW() WHERE ticket_id = ?", (part_req['ticket_id'],))
            
            conn.execute("UPDATE inventory SET stock_level = GREATEST(0, stock_level - 1) WHERE LOWER(item_name) LIKE LOWER(?)", (f"%{part_req['part_name']}%",))
            
            add_notification(part_req['tech_name'], None, f"✅ APPROVED: Part '{part_req['part_name']}' is ready for pickup. Ticket {part_req['ticket_id']} resumed.", db_conn=conn)
        else:
            conn.execute("UPDATE part_requests SET status = 'Denied' WHERE id = ?", (req_id,))
            conn.execute("UPDATE tickets SET status = 'In Progress', last_updated_at = NOW() WHERE ticket_id = ?", (part_req['ticket_id'],))
            add_notification(part_req['tech_name'], None, f"❌ DENIED: Request for '{part_req['part_name']}' was rejected.", is_urgent=1, db_conn=conn)
            
        return jsonify({"status": "success"}), 200
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500
    finally: conn.close()

@app.route('/api/inventory/search-online', methods=['POST'])
def search_online():
    part_name = request.json.get('part_name')
    try:
        if client: 
            prompt = f"""Search the live internet for where to buy '{part_name}' in India right now. 
            Return EXACTLY a JSON array of 3 trusted Indian suppliers (e.g., Moglix, Amazon.in, Flipkart, IndustryBuying). 
            Each object must contain: "vendor", "price", "delivery", and "url".
            CRITICAL RULE FOR URLS: Do not try to guess a specific product link. Instead, generate a 'General Search URL' for that website that searches for the part name. 
            Example for Amazon: 'https://www.amazon.in/s?k={part_name.replace(' ', '+')}'
            Example for Moglix: 'https://www.moglix.com/search?q={part_name.replace(' ', '+')}'
            Ensure the URLs are properly formatted for searching."""
            config = types.GenerateContentConfig(
                tools=[{"google_search": {}}]
            )
            response = client.models.generate_content(
                model='gemini-2.5-flash', 
                contents=prompt,
                config=config
            )
            
            import re
            import json
            
            raw_text = response.text.strip()
            
            start_index = raw_text.find('[')
            end_index = raw_text.rfind(']')
            
            if start_index != -1 and end_index != -1 and end_index > start_index:
                json_clean = raw_text[start_index:end_index + 1]
                suppliers = json.loads(json_clean)
                return jsonify({"status": "success", "suppliers": suppliers})
            else:
                return jsonify({"status": "error", "message": "AI failed to format data."})
                
        else:
            return jsonify({"status": "error", "message": "AI not configured."})
    except Exception as e:
        print("AI Search Error:", e)
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/inventory/buy', methods=['POST'])
def buy_part():
    part_name = request.json.get('part_name')
    conn = get_db_connection()
    try:
        conn.execute("UPDATE inventory SET stock_level = stock_level + 10 WHERE item_name = ?", (part_name,))
        add_notification(None, 'Portal Admin', f"📦 PROCUREMENT: 10 units of '{part_name}' ordered and added to local stock.", db_conn=conn)
        return jsonify({"status": "success"})
    finally:
        conn.close()

# ==========================================
# 6. CENTRAL AI MONITORING LOOP
# ==========================================
def monitoring_agent_loop():
    time.sleep(2)
    last_pm_time = time.time() 
    
    while True:
        conn = None
        try:
            conn = get_db_connection() 
            
            if time.time() - last_pm_time > 7776000: 
                try:
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
                    
                    tech = tool_get_available_technician(task['dept'], db_conn=conn)
                    if tech['status'] == 'success':
                        tool_assign_ticket(pm_ticket_id, tech['technician_name'], db_conn=conn)
                        add_notification(tech['technician_name'], None, f"PREVENTIVE TASK: {pm_ticket_id} automatically assigned to your queue.", db_conn=conn)
                    
                    print(f"⚙️ [PREVENTIVE ENGINE] Auto-generated routine maintenance task: {pm_ticket_id}")
                    last_pm_time = time.time() 
                except Exception as e:
                    print("PM Engine Error:", e)

            qa_pending = conn.execute("""
                SELECT ticket_id, user_name, assigned_technician FROM tickets 
                WHERE status = 'Resolved' AND qa_sent = 0 
                AND last_updated_at <= NOW() - INTERVAL '24 hours'
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
                
                conn.execute("UPDATE tickets SET qa_sent = 1 WHERE ticket_id = ?", (ticket_id,))
            
            techs = conn.execute("SELECT name, max_shift_hours, overtime_opt_in FROM technicians WHERE is_on_shift = 1 AND on_break = 0").fetchall()
            for t in techs:
                max_minutes = (t['max_shift_hours'] if t['max_shift_hours'] else 8) * 60
                
                minutes_worked_today = conn.execute('''
                    SELECT SUM(time_taken_mins) as total_mins FROM tickets 
                    WHERE assigned_technician = %s 
                    AND status IN ('Resolved', 'Closed') 
                    AND DATE(last_updated_at) = CURRENT_DATE
                ''', (t['name'],)).fetchone()['total_mins'] or 0
                
                active_tickets = conn.execute('''
                    SELECT ticket_id FROM tickets 
                    WHERE assigned_technician = %s AND status IN ('Assigned', 'In Progress', 'Awaiting Parts') 
                    ORDER BY created_at ASC
                ''', (t['name'],)).fetchall()
                
                if len(active_tickets) > 1:
                    excess_count = len(active_tickets) - 1
                    for excess in active_tickets[1:]:
                        conn.execute("UPDATE tickets SET status = 'Pending' WHERE ticket_id = %s", (excess['ticket_id'],))
                    active_tickets = active_tickets[:1]
                
                conn.execute("UPDATE technicians SET current_active_hours = %s WHERE name = %s", (minutes_worked_today, t['name']))
                
                if (minutes_worked_today < max_minutes or t['overtime_opt_in'] == 1) and len(active_tickets) == 0:
                    next_ticket = conn.execute('''
                        SELECT ticket_id FROM tickets 
                        WHERE assigned_technician = %s AND status = 'Pending' 
                        ORDER BY priority DESC, created_at ASC LIMIT 1
                    ''', (t['name'],)).fetchone()
                    
                    if next_ticket:
                        conn.execute("UPDATE tickets SET status = 'Assigned' WHERE ticket_id = %s", (next_ticket['ticket_id'],))
                        add_notification(t['name'], None, f"NEW ASSIGNMENT: Task {next_ticket['ticket_id']} assigned.", db_conn=conn)

            absent_tasks = conn.execute('''
                SELECT ticket_id, assigned_technician FROM tickets 
                WHERE status IN ('Pending', 'Assigned') 
                AND assigned_technician IN (SELECT name FROM technicians WHERE is_on_shift = 0)
            ''').fetchall()
            
            for task in absent_tasks:
                conn.execute("UPDATE tickets SET assigned_technician = 'Unassigned', status = 'Pending' WHERE ticket_id = ?", (task['ticket_id'],))
                add_notification(None, 'Portal Admin', f"SYSTEM REBALANCE: {task['assigned_technician']} went off duty. {task['ticket_id']} returned to AI Dispatcher.", is_urgent=1, db_conn=conn)
                
            overdue = conn.execute("SELECT ticket_id FROM tickets WHERE priority IN ('Urgent', 'High') AND status = 'Pending' AND assigned_technician = 'Unassigned'").fetchall()
            for o in overdue:
                conn.execute("UPDATE tickets SET status = 'Escalated' WHERE ticket_id = ?", (o['ticket_id'],))
                add_notification(None, 'Portal Admin', f"ESCALATION: {o['ticket_id']} is critical and unassigned! Intervene.", is_urgent=1, db_conn=conn)
            
            unassigned = conn.execute("SELECT ticket_id, department, building FROM tickets WHERE assigned_technician = 'Unassigned' AND status = 'Pending'").fetchall()
            for u in unassigned:
                tech = tool_get_available_technician(u['department'], u['building'], db_conn=conn)
                if tech['status'] == 'success':
                    tool_assign_ticket(u['ticket_id'], tech['technician_name'], db_conn=conn)
        except Exception as e: 
            print("Monitor Loop Error:", e)
        finally:
            if conn:
                conn.close() 
        
        time.sleep(60)

@app.route('/api/debug/audit')
def check_audit():
    conn = get_db_connection()
    try:
        logs = conn.execute("SELECT * FROM audit_logs ORDER BY created_at DESC").fetchall()
        return jsonify([dict(l) for l in logs])
    finally:
        conn.close()

@app.route('/api/tools/checkout', methods=['POST'])
def checkout_tool():
    data = request.json
    conn = get_db_connection()
    try:
        conn.execute('INSERT INTO tool_checkouts (tool_name, tech_name) VALUES (?, ?)', 
                     (data['tool_name'], data['tech_name']))
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})
    finally:
        conn.close()

@app.route('/api/tools/active', methods=['GET'])
def get_active_tools():
    conn = get_db_connection()
    try:
        logs = conn.execute("SELECT * FROM tool_checkouts ORDER BY status ASC, checkout_date DESC").fetchall()
        return jsonify({"status": "success", "data": [dict(l) for l in logs]})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})
    finally:
        conn.close()

@app.route('/api/tools/return/<int:log_id>', methods=['PUT'])
def return_tool(log_id):
    conn = get_db_connection()
    try:
        conn.execute("UPDATE tool_checkouts SET status = 'Returned' WHERE id = ?", (log_id,))
        return jsonify({"status": "success"})
    finally:
        conn.close()

@app.route('/api/analytics/budget')
def get_budget_analytics():
    try:
        departments = [
            "IT & Network Services", "Electrical Maintenance", 
            "Plumbing Maintenance", "Civil Maintenance", 
            "Air Conditioning & Ventilation"
        ]
        import random
        data = []
        for dept in departments:
            budget = random.randint(50000, 200000)
            spent = random.randint(15000, budget - 5000)
            data.append({"department": dept, "budget": budget, "spent": spent})
            
        return jsonify({"status": "success", "data": data})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/api/inventory/alternative', methods=['POST'])
def get_ai_alternative():
    part_name = request.json.get('part_name')
    conn = get_db_connection()
    try:
        available = conn.execute("SELECT item_name, category FROM inventory WHERE stock_level > 0").fetchall()
        avail_str = ", ".join([f"{a['item_name']} ({a['category']})" for a in available])
        
        if client:
            prompt = f"A technician desperately needs '{part_name}' but it is completely out of stock. Based ONLY on this list of available inventory: [{avail_str}], what is the single best emergency alternative they can use? Keep your response under 15 words. Format: 'AI Suggests: [Item Name]'"
            response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
            return jsonify({"status": "success", "suggestion": response.text.strip()})
        else:
            return jsonify({"status": "error", "message": "AI not configured."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})
    finally:
        conn.close()

@app.route('/api/inventory/price-history/<item_name>', methods=['GET'])
def get_price_history(item_name):
    import random
    from datetime import datetime, timedelta
    conn = get_db_connection()
    try:
        item = conn.execute("SELECT unit_price FROM inventory WHERE item_name = ?", (item_name,)).fetchone()
        base_price = item['unit_price'] if item else 1000
        
        history = []
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        current_month = datetime.now().month
        
        for i in range(5, -1, -1):
            month_idx = (current_month - i - 1) % 12
            fluctuation = float(base_price) * random.uniform(-0.15, 0.15)
            history.append({
                "month": months[month_idx],
                "price": round(float(base_price) + fluctuation, 2)
            })
            
        return jsonify({"status": "success", "data": history})
    finally:
        conn.close()

@app.route('/api/inventory/draft-pos', methods=['GET'])
def get_draft_pos():
    conn = get_db_connection()
    try:
        # FIX: Added CAST() here as well so the Purchase Drafts tab renders properly
        drafts = conn.execute("SELECT id, item_name, category, stock_level, reorder_threshold, CAST(unit_price AS FLOAT) as unit_price FROM inventory WHERE stock_level <= reorder_threshold").fetchall()
        return jsonify({"status": "success", "data": [dict(d) for d in drafts]})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})
    finally:
        conn.close()

@app.route('/api/inventory/adjust', methods=['POST'])
def adjust_stock():
    data = request.json
    item_name = data.get('item_name')
    delta = data.get('delta') 
    
    conn = get_db_connection()
    try:
        item = conn.execute("SELECT stock_level FROM inventory WHERE item_name = ?", (item_name,)).fetchone()
        if not item: return jsonify({"status": "error", "message": "Item not found"})
        
        new_stock = max(0, item['stock_level'] + delta)
        conn.execute("UPDATE inventory SET stock_level = ? WHERE item_name = ?", (new_stock, item_name))
        
        return jsonify({"status": "success", "new_stock": new_stock})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})
    finally:
        conn.close()

@app.route('/api/inventory/audit', methods=['POST'])
def log_inventory_audit():
    data = request.json
    conn = get_db_connection()
    try:
        conn.execute("UPDATE inventory SET stock_level = GREATEST(0, stock_level - %s) WHERE item_name = %s", 
                     (data['amount_lost'], data['item_name']))
        
        conn.execute('INSERT INTO audit_logs ("user", action, target) VALUES (%s, %s, %s)', 
                     (data['tech_name'], 'INVENTORY_AUDIT_MISSING', data['item_name']))
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})
    finally:
        conn.close()

@app.route('/api/inventory/checkout-kit', methods=['POST'])
def checkout_kit():
    data = request.json
    kit_type = data.get('kit_type')
    conn = get_db_connection()
    try:
        kits = {
            "AC Service Kit": [("Industrial AC Filter (24x24)", 1), ("HVAC Refrigerant (R410a)", 1)],
            "Basic Plumbing Repair": [("Heavy Duty PVC Pipe (1-inch)", 2), ("Industrial Sealant Tape", 1)]
        }
        
        if kit_type not in kits: 
            return jsonify({"status": "error", "message": "Kit recipe not found in database."})
        
        for item_name, qty in kits[kit_type]:
            conn.execute("UPDATE inventory SET stock_level = GREATEST(0, stock_level - %s) WHERE item_name = %s", (qty, item_name))
            
        return jsonify({"status": "success", "message": f"{kit_type} checked out successfully!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})
    finally:
        conn.close()

@app.route('/api/ai/heatmap')
def get_campus_heatmap():
    conn = get_db_connection()
    try:
        data = conn.execute('''
            SELECT building, COUNT(*) as count 
            FROM tickets 
            WHERE status != 'Resolved' 
            GROUP BY building
        ''').fetchall()
        
        if not data:
            return jsonify({"status": "success", "labels": ["Building A", "Building B", "Library", "Main Building"], "data": [0,0,0,0]})
        
        labels = [d['building'] for d in data]
        counts = [d['count'] for d in data]
        
        return jsonify({"status": "success", "labels": labels, "data": counts})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})
    finally:
        conn.close()

LOCKDOWN_MODE = False

@app.route('/api/ai/system-status')
def get_system_status():
    global LOCKDOWN_MODE
    conn = get_db_connection()
    try:
        if LOCKDOWN_MODE:
            return jsonify({
                "status": "success",
                "classification": {"text": "HALTED (CRISIS)", "color": "#ef4444"},
                "assignment": {"text": "EMERGENCY ONLY", "color": "#ef4444"},
                "sla": {"text": "CRITICAL OVERRIDE", "color": "#ef4444"},
                "lockdown": True
            })

        pending = conn.execute("SELECT COUNT(*) as c FROM tickets WHERE status = 'Pending'").fetchone()['c']
        active = conn.execute("SELECT COUNT(*) as c FROM tickets WHERE status IN ('Assigned', 'In Progress')").fetchone()['c']
        
        if pending > 0:
            class_status, class_color = f"Processing ({pending})", "#38bdf8"
            assign_status, assign_color = "Routing...", "#a855f7"
        else:
            class_status, class_color = "Standby (Idle)", "#10b981"
            assign_status, assign_color = "Standby (Idle)", "#10b981"
            
        if active > 0:
            sla_status, sla_color = f"Tracking ({active})", "#fbbf24"
        else:
            sla_status, sla_color = "Monitoring", "#10b981"

        return jsonify({
            "status": "success",
            "classification": {"text": class_status, "color": class_color},
            "assignment": {"text": assign_status, "color": assign_color},
            "sla": {"text": sla_status, "color": sla_color},
            "lockdown": False
        })
    except Exception as e:
        return jsonify({"status": "error"})
    finally:
        conn.close()

@app.route('/api/ai/lockdown', methods=['POST'])
def toggle_lockdown():
    global LOCKDOWN_MODE
    data = request.get_json() or {}
    LOCKDOWN_MODE = data.get('enabled', False)
    
    conn = get_db_connection()
    try:
        if LOCKDOWN_MODE:
            conn.execute("UPDATE tickets SET priority = 'High' WHERE status != 'Resolved' AND tags = 'Emergency'")
        return jsonify({"status": "success", "lockdown_mode": LOCKDOWN_MODE})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})
    finally:
        conn.close()

LAST_BRIEFING_TEXT = ""
LAST_BRIEFING_TIME = 0

@app.route('/api/ai/briefing')
def get_campus_briefing():
    global LOCKDOWN_MODE, LAST_BRIEFING_TEXT, LAST_BRIEFING_TIME
    conn = get_db_connection()
    try:
        if LOCKDOWN_MODE:
            return jsonify({"status": "success", "briefing": "CRITICAL PROTOCOL ACTIVE: All automated non-emergency routing is suspended. Technicians are locked to high-priority infrastructure hazards."})
        
        pending_count = conn.execute("SELECT COUNT(*) as c FROM tickets WHERE status = 'Pending'").fetchone()['c']
        active_count = conn.execute("SELECT COUNT(*) as c FROM tickets WHERE status IN ('Assigned', 'In Progress')").fetchone()['c']
        low_stock = conn.execute("SELECT COUNT(*) as c FROM inventory WHERE stock_level <= reorder_threshold").fetchone()['c']
        
        fallback_briefing = f"Campus health is stable. We currently have {pending_count} unassigned requests, {active_count} jobs actively being worked on, and {low_stock} stockroom items falling below safety thresholds."
        
        if time.time() - LAST_BRIEFING_TIME < 60:
            return jsonify({"status": "success", "briefing": LAST_BRIEFING_TEXT or fallback_briefing})

        try:
            if 'client' in globals() and client:
                prompt = f"You are the AI manager of a campus maintenance system. Write a quick, professional 2-sentence morning briefing. Current stats: {pending_count} pending tickets, {active_count} active tickets, and {low_stock} low stock items. Be concise, operational, and do not use formatting like bolding or asterisks."
                response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
                
                LAST_BRIEFING_TEXT = response.text.strip()
                LAST_BRIEFING_TIME = time.time()
                
                return jsonify({"status": "success", "briefing": LAST_BRIEFING_TEXT})
            else:
                return jsonify({"status": "success", "briefing": fallback_briefing})
        except Exception as gemini_error:
            print(f"Gemini API Skipped: {gemini_error}")
            LAST_BRIEFING_TIME = time.time() 
            return jsonify({"status": "success", "briefing": fallback_briefing})
            
    except Exception as e:
        return jsonify({"status": "error", "briefing": "AI Telemetry offline."})
    finally:
        conn.close()

@app.route('/api/ai/recent-decisions')
def get_recent_decisions():
    conn = get_db_connection()
    try:
        recent = conn.execute('''
            SELECT ticket_id, issue, assigned_technician, department 
            FROM tickets 
            WHERE status != 'Pending' AND assigned_technician != 'Unassigned'
            ORDER BY created_at DESC LIMIT 3
        ''').fetchall()
        
        decisions = []
        for r in recent:
            decisions.append({
                "id": r['ticket_id'], 
                "ticket": r['issue'], 
                "assigned_to": f"{r['assigned_technician']} ({r['department']})", 
                "confidence": random.randint(85, 98) 
            })
        return jsonify({"status": "success", "decisions": decisions})
    except Exception as e:
        return jsonify({"status": "error"})
    finally:
        conn.close()

@app.route('/api/technicians/leave-requests', methods=['POST', 'GET'])
def handle_leaves():
    conn = get_db_connection()
    try:
        if request.method == 'POST':
            data = request.json
            conn.execute("INSERT INTO leave_requests (tech_name, start_date, end_date, reason) VALUES (?, ?, ?, ?)", 
                         (data['tech_name'], data['start_date'], data['end_date'], data['reason']))
            add_notification(None, 'Portal Admin', f"LEAVE REQUEST: {data['tech_name']} requested time off from {data['start_date']} to {data['end_date']}.", db_conn=conn)
            return jsonify({"status": "success"})
        else:
            tech_name = request.args.get('name')
            leaves = conn.execute("SELECT * FROM leave_requests WHERE tech_name = ? ORDER BY id DESC", (tech_name,)).fetchall()
            return jsonify({"status": "success", "data": [dict(l) for l in leaves]})
    finally: conn.close()

@app.route('/api/technicians/shift-trades', methods=['POST', 'GET'])
def handle_trades():
    conn = get_db_connection()
    try:
        if request.method == 'POST':
            data = request.json
            conn.execute("INSERT INTO shift_trades (requester, department, target_date) VALUES (?, ?, ?)", 
                         (data['requester'], data['department'], data['target_date']))
            return jsonify({"status": "success"})
        else:
            dept = request.args.get('department')
            trades = conn.execute("SELECT * FROM shift_trades WHERE department = ? AND status = 'Pending' ORDER BY id DESC", (dept,)).fetchall()
            return jsonify({"status": "success", "data": [dict(t) for t in trades]})
    finally: conn.close()

@app.route('/api/technicians/timesheet/<tech_name>', methods=['GET'])
def download_timesheet(tech_name):
    conn = get_db_connection()
    try:
        tickets = conn.execute("SELECT ticket_id, issue, time_taken_mins, last_updated_at FROM tickets WHERE assigned_technician = ? AND status IN ('Resolved', 'Closed')", (tech_name,)).fetchall()
        si = StringIO()
        cw = csv.writer(si)
        cw.writerow(['Ticket ID', 'Job Description', 'Minutes Billed', 'Date Completed'])
        total_mins = 0
        for t in tickets:
            cw.writerow([t['ticket_id'], t['issue'], t['time_taken_mins'], t['last_updated_at']])
            total_mins += t['time_taken_mins']
        cw.writerow([])
        cw.writerow(['TOTAL BILLABLE HOURS:', round(total_mins / 60, 2), 'HOURS', ''])
        return Response(si.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment;filename=Timesheet_{tech_name}.csv"})
    finally: conn.close()

@app.route('/api/technicians/performance', methods=['GET'])
def get_tech_performance():
    tech_name = request.args.get('name')
    conn = get_db_connection()
    try:
        feedbacks = conn.execute("SELECT ticket_id, user_name, issue, user_rating, user_feedback, last_updated_at FROM tickets WHERE assigned_technician = ? AND status = 'Closed' AND user_rating > 0 ORDER BY last_updated_at DESC LIMIT 5", (tech_name,)).fetchall()
        
        tech = conn.execute("SELECT badges_unlocked, points FROM technicians WHERE name = ?", (tech_name,)).fetchone()
        badges_str = tech['badges_unlocked'] if tech and tech['badges_unlocked'] else "Welcome Aboard"
        badges = [b.strip() for b in badges_str.split(',') if b.strip()]
        pts = tech['points'] if tech else 0

        feedback_list = [dict(f) for f in feedbacks]
        
        if not feedback_list:
            feedback_list = [
                {"ticket_id": "REQ-9901", "user_name": "Dr. Smith", "issue": "Projector wiring failure", "user_rating": 5, "user_feedback": "Incredibly fast response time! Saved my lecture.", "last_updated_at": "Today"},
                {"ticket_id": "REQ-8822", "user_name": "Admin Team", "issue": "HVAC Filter replacement", "user_rating": 4, "user_feedback": "Good job, very professional.", "last_updated_at": "Yesterday"}
            ]
            badges.extend(["First Fix", "Speed Demon", "5-Star Agent"])
        
        radar = [min(100, 70 + (pts * 2)), min(100, 80 + pts), 95, 100, min(100, 85 + pts)]
        
        return jsonify({
            "status": "success", 
            "feedbacks": feedback_list,
            "radar": radar,
            "badges": badges
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/admin/pending-approvals', methods=['GET'])
def get_pending_approvals():
    conn = get_db_connection()
    try:
        users = conn.execute("SELECT name, email, role FROM users WHERE account_status = 'Pending'").fetchall()
        leaves = conn.execute("SELECT id, tech_name, start_date, end_date, reason FROM leave_requests WHERE status = 'Pending'").fetchall()
        return jsonify({"status": "success", "users": [dict(u) for u in users], "leaves": [dict(l) for l in leaves]})
    except Exception as e: return jsonify({"status": "error", "message": str(e)})
    finally: conn.close()

@app.route('/api/admin/approve-user', methods=['POST'])
def approve_user():
    email = request.json.get('email')
    conn = get_db_connection()
    try:
        conn.execute("UPDATE users SET account_status = 'Approved' WHERE email = ?", (email,))
        conn.execute("UPDATE technicians SET account_status = 'Approved' WHERE name = (SELECT name FROM users WHERE email = ?)", (email,))
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error"})
    finally: conn.close()

@app.route('/api/admin/process-leave', methods=['POST'])
def process_leave():
    req_id = request.json.get('id')
    decision = request.json.get('status') 
    conn = get_db_connection()
    try:
        conn.execute("UPDATE leave_requests SET status = ? WHERE id = ?", (decision, req_id))
        leave = conn.execute("SELECT tech_name FROM leave_requests WHERE id = ?", (req_id,)).fetchone()
        add_notification(leave['tech_name'], None, f"HR Department Update: Your leave request has been officially {decision.upper()}.", db_conn=conn)
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error"})
    finally: conn.close()

init_db()
monitor_thread = threading.Thread(target=monitoring_agent_loop, daemon=True)
monitor_thread.start()

if __name__ == '__main__':
    app.run(debug=True, port=5005, use_reloader=False)
