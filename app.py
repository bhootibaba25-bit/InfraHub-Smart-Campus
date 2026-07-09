import os
import json
import threading
import time
import random
import csv
import re
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
from psycopg2 import pool
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator
from functools import wraps
from werkzeug.middleware.proxy_fix import ProxyFix
from twilio.rest import Client

# ==========================================
# 1. SECURE SETUP & EMAIL CONFIGURATION
# ==========================================
load_dotenv()
API_KEY = os.getenv("GEMINI_API_KEY")
app = Flask(__name__)

# Serve the main HTML page
@app.route('/')
def home():
    return send_from_directory('public', 'index.html')

CORS(app)
# Helper to safely initialize a client or fall back to the main key if one isn't provided
def create_task_client(specific_env_var):
    key = os.getenv(specific_env_var) or os.getenv("GEMINI_API_KEY")
    if key:
        return genai.Client(api_key=key)
    return None

# 8 Dedicated AI Clients for 8 Isolated Tasks
AI_POOL = {
    "classify": create_task_client("GEMINI_KEY_CLASSIFY"),
    "quick_fix": create_task_client("GEMINI_KEY_QUICKFIX"),
    "summarize": create_task_client("GEMINI_KEY_SUMMARIZE"),
    "email_draft": create_task_client("GEMINI_KEY_EMAILDRAFT"),
    "market_search": create_task_client("GEMINI_KEY_MARKET"),
    "alternative": create_task_client("GEMINI_KEY_ALTERNATIVE"),
    "chat": create_task_client("GEMINI_KEY_CHAT"),
    "briefing": create_task_client("GEMINI_KEY_BRIEFING")
}

app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.getenv("MAIL_USERNAME")
app.config['MAIL_PASSWORD'] = os.getenv("MAIL_PASSWORD")
mail = Mail(app)


# ==========================================
# TWILIO CLIENT INITIALIZATION
# ==========================================
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = "whatsapp:+14155238886" # Standard Twilio Sandbox Number

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID else None
# System Briefing Cache
LAST_BRIEFING_TEXT = ""
LAST_BRIEFING_TIME = 0



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
            WHERE t.ticket_id = %s
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

# Initialize connection pool globally
db_pool = None

def init_pool():
    global db_pool
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is missing! Add your Neon Postgres URL to Render.")
    # Keep up to 20 database connections open permanently for instant response times
    db_pool = psycopg2.pool.ThreadedConnectionPool(1, 20, db_url)

class DBConnection:
    """A brilliant custom wrapper that uses Connection Pooling for massive speedups!"""
    def __init__(self):
        if not db_pool:
            init_pool()
        # Instantly grab an already-open connection from the pool
        self.conn = db_pool.getconn()
        self.conn.autocommit = True 

    
    def execute(self, query, params=()):
        # PostgreSQL uses %s placeholders, NOT ?
        pg_query = query.replace('?', '%s')
        
        cursor = self.conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(pg_query, params)
        return cursor
    
    def executemany(self, query, param_list):
        pg_query = query.replace('%s', '%s')
        cursor = self.conn.cursor()
        cursor.executemany(pg_query, param_list)
        return cursor

    def commit(self):
        pass

    def close(self):
        # Crucial: Give the connection back to the pool instead of destroying it!
        if self.conn:
            db_pool.putconn(self.conn)
            self.conn = None

def get_db_connection():
    return DBConnection()

def init_db():
    conn = get_db_connection()
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, custom_id TEXT, name TEXT, email TEXT UNIQUE, password TEXT, role TEXT, mobile_no TEXT, account_status TEXT DEFAULT 'Approved')''')
        conn.execute('''CREATE TABLE IF NOT EXISTS technicians (technician_id SERIAL PRIMARY KEY, custom_id TEXT, name TEXT, department TEXT, current_active_hours INTEGER DEFAULT 0, max_shift_hours INTEGER DEFAULT 8, is_on_shift INTEGER DEFAULT 0, mobile_no TEXT, points INTEGER DEFAULT 0, on_break INTEGER DEFAULT 0, overtime_opt_in INTEGER DEFAULT 0, badges_unlocked TEXT DEFAULT 'Welcome Aboard', current_building TEXT DEFAULT 'Main Building', account_status TEXT DEFAULT 'Approved')''')
        conn.execute('''CREATE TABLE IF NOT EXISTS inventory (id SERIAL PRIMARY KEY, item_name TEXT, category TEXT, stock_level INTEGER DEFAULT 0, reorder_threshold INTEGER DEFAULT 5, unit_price NUMERIC)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS part_requests (id SERIAL PRIMARY KEY, ticket_id TEXT, tech_name TEXT, part_name TEXT, status TEXT DEFAULT 'Pending', requested_at TIMESTAMP DEFAULT NOW())''')
        conn.execute('''CREATE TABLE IF NOT EXISTS audit_logs (id SERIAL PRIMARY KEY, "user" TEXT, action TEXT, target TEXT, created_at TIMESTAMP DEFAULT NOW())''')
        conn.execute('''CREATE TABLE IF NOT EXISTS shift_trades (id SERIAL PRIMARY KEY, requester TEXT, department TEXT, target_date TEXT, status TEXT DEFAULT 'Pending')''')
        conn.execute('''CREATE TABLE IF NOT EXISTS leave_requests (id SERIAL PRIMARY KEY, tech_name TEXT, start_date TEXT, end_date TEXT, reason TEXT, status TEXT DEFAULT 'Pending')''')
        conn.execute('''CREATE TABLE IF NOT EXISTS tool_checkouts (id SERIAL PRIMARY KEY, tool_name TEXT, tech_name TEXT, checkout_date TIMESTAMP DEFAULT NOW(), status TEXT DEFAULT 'Borrowed')''')
        conn.execute('''CREATE TABLE IF NOT EXISTS password_resets (email TEXT PRIMARY KEY, otp TEXT, created_at TIMESTAMP DEFAULT NOW())''')
        conn.execute('''CREATE TABLE IF NOT EXISTS whatsapp_sessions (phone_number TEXT PRIMARY KEY, current_state TEXT DEFAULT 'IDLE', temp_issue TEXT, temp_building TEXT, last_interaction TIMESTAMP DEFAULT NOW())''')
        conn.execute('''CREATE TABLE IF NOT EXISTS tickets (ticket_id TEXT PRIMARY KEY, user_name TEXT, contact_number TEXT, role TEXT, department TEXT, building TEXT, location TEXT, issue TEXT, photo_attached TEXT, priority TEXT, ai_analysis TEXT, assigned_technician TEXT DEFAULT 'Unassigned', status TEXT DEFAULT 'Pending', decline_reason TEXT, read_status INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT NOW(), last_updated_at TIMESTAMP DEFAULT NOW(), qa_sent INTEGER DEFAULT 0, started_at TIMESTAMP, time_taken_mins INTEGER DEFAULT 0, user_rating INTEGER DEFAULT 0, user_feedback TEXT, resolution_photo TEXT DEFAULT 'None')''')

        

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
            conn.executemany("INSERT INTO inventory (item_name, category, stock_level, reorder_threshold, unit_price) VALUES (%s, %s, %s, %s, %s)", seed_items)

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
                             VALUES (%s, %s, 'Tech123!', 'Campus Technician', 'Approved')''', (tech_name, tech_email))
                conn.execute('''INSERT INTO technicians (name, department, is_on_shift, account_status) 
                             VALUES (%s, %s, 0, 'Approved')''', (tech_name, dept))

    except Exception as e:
        print("Init DB Error:", e)
    finally:
        conn.close()
        
def log_audit(user, action, target, db_conn=None):
    conn = db_conn or get_db_connection()
    try:
        conn.execute('INSERT INTO audit_logs ("user", action, target) VALUES (%s, %s, %s)', (user, action, target))
    except Exception as e:
        print("Audit Log Error:", e)
    finally:
        if not db_conn and conn: 
            conn.close()

def add_notification(target_user, target_role, message, is_urgent=0, db_conn=None):
    conn = db_conn or get_db_connection()
    try:
        conn.execute('INSERT INTO system_notifications (target_user, target_role, message, is_urgent) VALUES (%s, %s, %s, %s)', (target_user, target_role, message, is_urgent))
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
                   (t.current_building = %s) as is_close,
                   (SELECT COUNT(*) FROM tickets WHERE assigned_technician = t.name AND status IN ('Assigned', 'In Progress', 'Pending')) as total_tasks
            FROM technicians t
            WHERE (t.department = %s OR t.department LIKE %s) AND t.is_on_shift = 1 AND t.on_break = 0
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
        tech = conn.execute("SELECT current_active_hours, max_shift_hours FROM technicians WHERE name = %s", (technician_name,)).fetchone()
        
        max_hrs = tech['max_shift_hours'] if tech and tech['max_shift_hours'] else 8
        current_hrs = tech['current_active_hours'] if tech and tech['current_active_hours'] else 0
        
        if tech and (current_hrs + estimated_task_hours) <= max_hrs:
            conn.execute("UPDATE tickets SET assigned_technician = %s, status = 'Assigned' WHERE ticket_id = %s", (technician_name, ticket_id))
            notify_status_change(ticket_id, 'Assigned', db_conn=conn)
        else:
            conn.execute("UPDATE tickets SET assigned_technician = %s, status = 'Pending' WHERE ticket_id = %s", (technician_name, ticket_id))
            
        return {"status": "success"}
    finally:
        if not db_conn and conn:
            conn.close()


# ==========================================
# 4. NATURAL LANGUAGE CLASSIFICATION AGENT
# ==========================================
def classify_ticket_with_ai(issue):
    task_client = AI_POOL.get("classify")
    if task_client:
        try:
            # A deeply analytical prompt establishing clear borders between all 10 departments
            prompt = f"""
            You are the expert AI Dispatcher Node for a comprehensive smart campus facility management system. 
            Analyze the user's natural language complaint deeply and categorize it thoroughly. 
            
            User Complaint: "{issue}"
            
            You must map this complaint to EXACTLY one of the following 10 departments based on these operational scopes:
            1. "IT & Network Services": Wi-Fi drops, internet connectivity, routers, switches, ethernet ports, servers, or computing hardware errors.
            2. "Electrical Maintenance": Short circuits, flickering lights, dead power sockets, blown fuses, electrical panels, wiring problems, or total room blackouts.
            3. "Plumbing Maintenance": Dripping faucets, clogged washbasins, running flush tanks, broken pipe valves, bathroom blockages, or minor localized leaks.
            4. "Civil Maintenance": Cracks in walls, ceiling plaster peeling, broken doors/windows, tile damage, furniture repairs, lock replacements, masonry, or structural wear.
            5. "Air Conditioning & Ventilation Services": AC compressor failure, water leaking from indoor units, lack of cooling, severe vent rattle, duct blockages, or fan failures.
            6. "Security & Surveillance": Damaged CCTV cameras, digital card reader lock failures, biometric sensor errors, perimeter gate automation faults, or intercom dead lines.
            7. "Housekeeping Services": Liquid spills, garbage accumulation, corridor cleaning, window washing requests, dirty spaces, or emergency pest control.
            8. "Fire Safety Systems": Fire extinguisher pressure drops, smoke detector warning chirps, malfunctioning sprinklers, or emergency exit sign electrical defects.
            9. "Water Supply & Sewage Management": Total loss of building water supply, main line sewage back-ups, overhead water tank overflow sensors tripping, or pump station pipeline bursts.
            10. "Equipment Support": Broken classroom projectors, non-functional smart boards, audio system microphone failures, lab instruments, or educational equipment defects.

            Priority Matrix Guidelines:
            - "Low": Isolated, minor issues that do not interrupt work or classes.
            - "Medium": Noticeable disruptions that reduce comfort but allow space usage.
            - "High": Significant class or operational office halts (e.g., classroom projector dead, no lights in an ongoing lab).
            - "Urgent": Immediate physical safety hazards, active severe structural flooding, structural collapse dangers, or a total building systems failure.

            Return EXACTLY a valid JSON object with these 3 keys and no extra formatting:
            {{
                "department": "Exact string matching one of the 10 teams listed above",
                "priority": "Low", "Medium", "High", or "Urgent",
                "ai_analysis": "A thorough, 1-sentence technical justification of your chosen department and priority ranking."
            }}

            CRITICAL: Do not wrap your response in markdown code blocks like ```json ... ```. Provide the raw JSON text string directly.
            """
            
            response = task_client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
            raw_text = response.text.strip()
            
            # Resilient JSON parsing to catch hidden brackets
            start_idx = raw_text.find('{')
            end_idx = raw_text.rfind('}')
            
            if start_idx != -1 and end_idx != -1:
                json_clean = raw_text[start_idx:end_idx + 1]
                return json.loads(json_clean)
                
        except Exception as e:
            print(f"Thorough NLP Router Error: {e}")
            
    # System recovery default if AI fails completely 
    return {
        "department": "Civil Maintenance", 
        "priority": "Medium", 
        "ai_analysis": "CRITICAL ENGINE FALLBACK: Automated routing error. Assigned to general Civil queue for manual triaging."
    }
# ==========================================
# 5. REST API ROUTES & ENDPOINTS
# ==========================================
@app.route('/<path:filename>')
def serve_static_file(filename): 
    # Create a folder named 'public' in your root directory and put index.html there
    return send_from_directory('public', filename)

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    conn = get_db_connection()
    try:
        requested_role = data.get('role')
        status = 'Pending' if requested_role in ['Portal Admin', 'Campus Technician', 'Master Technician'] else 'Approved'
            
        conn.execute('INSERT INTO users (name, email, password, role, account_status) VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING', 
                     (data['name'], data['email'], data['password'], requested_role, status))
        
        if requested_role == 'Campus Technician':
            dept = data.get('department', 'Pending Assignment')
            conn.execute('INSERT INTO technicians (name, department, current_active_hours, max_shift_hours, is_on_shift, account_status) VALUES (%s, %s, %s, %s, %s, %s)', 
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
        user = conn.execute('SELECT * FROM users WHERE LOWER(TRIM(email)) = %s AND password = %s', (email, password)).fetchone()
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
        user = conn.execute('SELECT * FROM users WHERE LOWER(email) = LOWER(%s)', (email,)).fetchone()
        if not user: 
            return jsonify({"status": "error", "message": "Email not found in our database."})
        
        otp = str(random.randint(100000, 999999))
        conn.execute('INSERT INTO password_resets (email, otp) VALUES (%s, %s) ON CONFLICT (email) DO UPDATE SET otp = EXCLUDED.otp, created_at = NOW()', (email.lower(), otp))
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
    email = data.get('email', '').lower()
    otp = data.get('otp')
    new_password = data.get('new_password')

    conn = get_db_connection()
    try:
        # Check DB instead of local memory dictionary
        record = conn.execute('SELECT otp FROM password_resets WHERE email = %s', (email,)).fetchone()

        if record and record['otp'] == otp:
            conn.execute('UPDATE users SET password = %s WHERE LOWER(email) = %s', (new_password, email))
            user = conn.execute('SELECT * FROM users WHERE LOWER(email) = %s', (email,)).fetchone()
            if user: 
                add_notification(user['name'], None, "Security Alert: Your password was recently changed.", db_conn=conn)

            # Delete the used OTP
            conn.execute('DELETE FROM password_resets WHERE email = %s', (email,))
            return jsonify({"status": "success", "message": "Password reset successfully."})
        else: 
            return jsonify({"status": "error", "message": "Invalid or expired OTP."}), 400
    except Exception as e: 
        return jsonify({"status": "error", "message": "Database error during reset."}), 500
    finally:
        conn.close()

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

@app.route('/api/ai/chat', methods=['POST'])
def ai_custom_chat():
    data = request.json
    user_message = data.get('message', '')
    user_name = data.get('user_name', 'User')
    user_role = data.get('role', 'Campus Staff')

    conn = get_db_connection()
    try:
        live_context = ""
        
        # 1. Base Stats (Keep the general counts)
        if user_role in ['Portal Admin', 'Master Admin']:
            pending = conn.execute("SELECT COUNT(*) as c FROM tickets WHERE status = 'Pending'").fetchone()['c']
            active = conn.execute("SELECT COUNT(*) as c FROM tickets WHERE status IN ('Assigned', 'In Progress')").fetchone()['c']
            live_context += f"System Stats: {pending} pending, {active} active.\n"
        elif user_role in ['Campus Technician', 'Master Technician']:
            my_tasks = conn.execute("SELECT COUNT(*) as c FROM tickets WHERE assigned_technician = %s AND status IN ('Assigned', 'In Progress', 'Pending')", (user_name,)).fetchone()['c']
            live_context += f"Technician Stats: You have {my_tasks} active tasks.\n"
        else:
            my_tickets = conn.execute("SELECT COUNT(*) as c FROM tickets WHERE user_name = %s AND status NOT IN ('Resolved', 'Closed', 'Cancelled')", (user_name,)).fetchone()['c']
            live_context += f"User Stats: You have {my_tickets} active requests.\n"

        # 2. Dynamic Database Search (The Upgraded Memory)
        search_results = []
        
        # A. Look for specific Ticket IDs in the chat (e.g., req-8342)
        ticket_matches = re.findall(r'req-\d+', user_message, re.IGNORECASE)
        if ticket_matches:
            for t_id in ticket_matches:
                t_record = conn.execute("SELECT * FROM tickets WHERE ticket_id ILIKE %s", (t_id,)).fetchone()
                if t_record:
                    search_results.append(t_record)
        
        # B. Look for keywords (words longer than 3 letters like "smart", "room", "315")
        words = [w for w in re.findall(r'\b\w+\b', user_message) if len(w) > 3]
        if words:
            conditions = " OR ".join(["issue ILIKE %s OR location ILIKE %s" for _ in words])
            params = []
            for w in words:
                params.extend([f"%{w}%", f"%{w}%"])
            
            query = f"SELECT * FROM tickets WHERE {conditions} ORDER BY created_at DESC LIMIT 5"
            word_records = conn.execute(query, tuple(params)).fetchall()
            
            for w_rec in word_records:
                if not any(sr['ticket_id'] == w_rec['ticket_id'] for sr in search_results):
                    search_results.append(w_rec)

        # C. Inject findings into Gemini's context window
        if search_results:
            live_context += "\nRelevant Database Records Found For This Query:\n"
            for r in search_results:
                live_context += f"- Ticket {r['ticket_id']}: '{r['issue']}' | Loc: {r['building']} {r['location']} | Status: {r['status']} | Tech: {r['assigned_technician']}\n"
        else:
            # Fallback: Just give Gemini the 3 most recent tickets to talk about
            recent = conn.execute("SELECT ticket_id, issue, status, building, location FROM tickets ORDER BY created_at DESC LIMIT 3").fetchall()
            if recent:
                live_context += "\nRecent Campus Ticket History:\n"
                for r in recent:
                    live_context += f"- Ticket {r['ticket_id']}: '{r['issue']}' | Loc: {r['building']} {r['location']} | Status: {r['status']}\n"

        # 3. Send to Gemini
        task_client = AI_POOL.get("chat")
        if task_client:
            prompt = f"""
            You are 'InfraHub Nexus', the highly advanced, professional, and slightly futuristic AI assistant for a smart campus maintenance portal.
            The person talking to you is {user_name}, and their system role is {user_role}. 
            
            [LIVE SYSTEM CONTEXT (Do not mention this context unless asked. It contains real database records pulled specifically for this user's query)]: 
            {live_context}
            
            Answer the user's query intelligently, directly, and concisely. 
            - If they ask general knowledge questions, answer them normally.
            - If they ask about their tickets, tasks, or specific items (like a TV or a REQ- number), rely ONLY on the 'Relevant Database Records Found' provided in the LIVE SYSTEM CONTEXT. 
            
            User's Query: "{user_message}"
            """
            
            response = task_client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
            return jsonify({"status": "success", "reply": response.text.strip()})
        else:
            return jsonify({"status": "error", "reply": "My AI neural net is currently offline. Please check the API key."})
            
    except Exception as e:
        print(f"Chat AI Error: {e}")
        return jsonify({"status": "error", "reply": f"My neural net is experiencing interference: {str(e)}"})
    finally:
        conn.close()       

@app.route('/api/ai/quick-fix', methods=['POST'])
def ai_quick_fix():
    issue = request.json.get('issue')
    try:
        task_client = AI_POOL.get("quick_fix")
        if task_client:
            prompt = f"A campus user is about to submit a maintenance ticket for this issue: '{issue}'. Give them a brief, friendly, 2-sentence DIY troubleshooting tip they can try right now to fix it themselves before calling a technician. Keep it safe and practical."
            response = task_client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
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
            WHERE building = %s AND location = %s AND department = %s AND status NOT IN ('Resolved', 'Closed', 'Cancelled')
        ''', (bldg, loc, ticket_dept)).fetchone()

        if duplicate_check:
            return jsonify({
                "status": "error",
                "message": f"Duplicate Prevented: Our {ticket_dept} team is already working on an active issue in {bldg} - {loc}!"
            }), 400
        
        conn.execute('INSERT INTO tickets (ticket_id, user_name, role, department, building, location, issue, photo_attached, priority, ai_analysis) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)', 
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
            tickets = conn.execute("SELECT * FROM tickets WHERE assigned_technician = %s AND status != 'Cancelled' ORDER BY created_at DESC", (user_name,)).fetchall()
        else: 
            tickets = conn.execute("SELECT * FROM tickets WHERE user_name = %s AND status != 'Cancelled' ORDER BY created_at DESC", (user_name,)).fetchall()
            
        return jsonify({"status": "success", "data": [dict(t) for t in tickets]}), 200
    except Exception as e: 
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/tickets/<ticket_id>', methods=['DELETE'])
def delete_ticket(ticket_id):
    conn = get_db_connection()
    try:
        conn.execute("UPDATE tickets SET status = 'Cancelled' WHERE ticket_id = %s", (ticket_id,))
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
            tech = conn.execute("SELECT is_on_shift FROM technicians WHERE name = %s", (tech_name,)).fetchone()
            if tech and tech['is_on_shift'] == 0:
                return jsonify({"status": "error", "message": "Action Denied: You cannot accept tasks while Off Duty. Please Clock In first."}), 403

        ticket = conn.execute('SELECT * FROM tickets WHERE ticket_id = %s', (ticket_id,)).fetchone()
        
        # THE FIX: We must update BOTH the status and the assigned_technician column!
        if tech_name:
            conn.execute("UPDATE tickets SET status = 'In Progress', assigned_technician = %s, started_at = NOW() WHERE ticket_id = %s", (tech_name, ticket_id))
        else:
            conn.execute("UPDATE tickets SET status = 'In Progress', started_at = NOW() WHERE ticket_id = %s", (ticket_id,))
        
        if ticket: 
            add_notification(ticket['user_name'], None, f"IN PROGRESS: Technician has started working on {ticket_id}.", db_conn=conn)
            notify_status_change(ticket_id, 'In Progress', db_conn=conn)
            
            # ---> FIXED WHATSAPP CODE HERE <---
            # If the user's name implies they came from WhatsApp, send them a live update!
            if "WhatsApp" in ticket['user_name'] and ticket.get('contact_number'):
                send_whatsapp_update(f"whatsapp:+{ticket['contact_number']}", ticket_id, "In Progress")
            # ------------------------------------------
        
        return jsonify({"status": "success"}), 200
    except Exception as e: 
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()
        
@app.route('/api/tickets/<ticket_id>/start', methods=['PUT'])
def start_ticket(ticket_id):
    conn = get_db_connection()
    try:
        conn.execute("UPDATE tickets SET status = 'In Progress', started_at = NOW(), last_updated_at = NOW() WHERE ticket_id = %s", (ticket_id,))
        return jsonify({"status": "success"}), 200
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500
    finally: conn.close()

@app.route('/api/tickets/<ticket_id>/resolve', methods=['PUT'])
def resolve_ticket(ticket_id):
    conn = get_db_connection()
    try:
        res_photo = request.json.get('resolution_photo', 'None')
        conn.execute('''
            UPDATE tickets 
            SET status = 'Resolved', 
                resolution_photo = %s,
                time_taken_mins = COALESCE(CAST(EXTRACT(EPOCH FROM (NOW() - COALESCE(started_at, created_at))) / 60 AS INTEGER), 1),
                last_updated_at = NOW() 
            WHERE ticket_id = %s
        ''', (res_photo, ticket_id))

        # ---> FIXED SQL QUERY TO INCLUDE contact_number <---
        ticket = conn.execute("SELECT user_name, contact_number, time_taken_mins FROM tickets WHERE ticket_id = %s", (ticket_id,)).fetchone()
        
        if ticket:
            add_notification(ticket['user_name'], None, f"Your request {ticket_id} has been resolved! It took our team {ticket['time_taken_mins']} minutes to fix.", db_conn=conn)

            # ---> FIXED WHATSAPP CODE HERE <---
            if "WhatsApp" in ticket['user_name'] and ticket.get('contact_number'):
                send_whatsapp_update(f"whatsapp:+{ticket['contact_number']}", ticket_id, "Resolved", "Check your email for the QA Survey!")
            # ------------------------------------------

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
        conn.execute("UPDATE tickets SET department = %s, assigned_technician = 'Unassigned', status = 'Pending', last_updated_at = NOW() WHERE ticket_id = %s", (new_dept, ticket_id))

        add_notification(None, 'Portal Admin', f"TICKET RE-ROUTED: {tech_name} transferred {ticket_id} to {new_dept}. Reason: {reason}", is_urgent=1, db_conn=conn)
        ticket = conn.execute("SELECT user_name FROM tickets WHERE ticket_id = %s", (ticket_id,)).fetchone()
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
        
        conn.execute("UPDATE tickets SET status = 'Awaiting Parts', last_updated_at = NOW() WHERE ticket_id = %s", (ticket_id,))
        conn.execute("INSERT INTO part_requests (ticket_id, tech_name, part_name) VALUES (%s, %s, %s)", (ticket_id, tech_name, part_name))
        
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
        ticket = conn.execute('SELECT * FROM tickets WHERE ticket_id = %s', (ticket_id,)).fetchone()
        conn.execute("UPDATE tickets SET status = 'Decline Requested', decline_reason = %s WHERE ticket_id = %s", (reason, ticket_id))
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
        ticket = conn.execute("SELECT * FROM tickets WHERE ticket_id = %s", (ticket_id,)).fetchone()
        if ticket:
            old_tech = ticket['assigned_technician']
            dept = ticket['department']
            
            new_techs = conn.execute('''
                SELECT t.name, 
                       (SELECT COUNT(*) FROM tickets WHERE assigned_technician = t.name AND status IN ('Assigned', 'In Progress', 'Pending')) as total_tasks
                FROM technicians t
                WHERE (t.department = %s OR t.department LIKE %s) AND t.is_on_shift = 1 AND t.name != %s
                ORDER BY total_tasks ASC
            ''', (dept, f'%{dept}%', old_tech)).fetchall()
            
            if new_techs:
                new_tech_name = new_techs[0]['name']
                conn.execute("UPDATE tickets SET assigned_technician = %s, status = 'Pending', decline_reason = NULL WHERE ticket_id = %s", (new_tech_name, ticket_id))
                add_notification(old_tech, None, f"Admin Approved: You have been removed from ticket {ticket_id}.", db_conn=conn)
                add_notification(new_tech_name, None, f"REASSIGNED TASK: {ticket_id} has been added to your queue.", db_conn=conn)
                add_notification(ticket['user_name'], None, f"Update: {ticket_id} reassigned to {new_tech_name}.", db_conn=conn)
            else:
                conn.execute("UPDATE tickets SET assigned_technician = 'Unassigned', status = 'Pending', decline_reason = NULL WHERE ticket_id = %s", (ticket_id,))
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
        ticket = conn.execute("SELECT * FROM tickets WHERE ticket_id = %s", (ticket_id,)).fetchone()
        conn.execute("UPDATE tickets SET status = 'Assigned', decline_reason = NULL WHERE ticket_id = %s", (ticket_id,))
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
            notifs = conn.execute("SELECT * FROM system_notifications WHERE target_user = %s OR target_role = 'Portal Admin' ORDER BY id DESC LIMIT 50", (name,)).fetchall()
        else: 
            notifs = conn.execute("SELECT * FROM system_notifications WHERE target_user = %s ORDER BY id DESC LIMIT 50", (name,)).fetchall()
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
        conn.execute("UPDATE system_notifications SET is_read = 1 WHERE target_user = %s OR target_role = %s", (data['name'], data['role']))
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
        ticket = conn.execute("SELECT * FROM tickets WHERE ticket_id = %s", (ticket_id,)).fetchone()
        if not ticket:
            return "<h1>Error</h1><p>Ticket not found.</p>", 404
            
        if answer == 'yes':
            conn.execute('''UPDATE tickets 
                            SET status = 'Closed', last_updated_at = NOW(), 
                                user_rating = %s, user_feedback = %s 
                            WHERE ticket_id = %s''', (rating, feedback, ticket_id))
            
            points_awarded = 10 + (rating * 2) 
            conn.execute("UPDATE technicians SET points = points + %s WHERE name = %s", (points_awarded, ticket['assigned_technician'])) 
            
            add_notification(ticket['assigned_technician'], None, f"🎉 QA Passed! +{points_awarded} Points. User rated you {rating} Stars!", db_conn=conn)
            return f"<h1 style='color: #10b981; font-family: sans-serif;'>Thank you!</h1><p style='font-family: sans-serif;'>Your {rating}-star rating has been recorded for {ticket['assigned_technician']}. Have a great day!</p>"
        
        else:
            conn.execute("UPDATE tickets SET status = 'Escalated', priority = 'Urgent', last_updated_at = NOW() WHERE ticket_id = %s", (ticket_id,))
            conn.execute("UPDATE technicians SET points = points - 5 WHERE name = %s", (ticket['assigned_technician'],)) 
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
        tech = conn.execute("SELECT is_on_shift FROM technicians WHERE name = %s", (tech_name,)).fetchone()
        if not tech:
            return jsonify({"status": "error", "message": "Technician not found."}), 404
            
        new_status = 0 if tech['is_on_shift'] == 1 else 1

        conn.execute("UPDATE technicians SET is_on_shift = %s WHERE name = %s", (new_status, tech_name))
        
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
        items = conn.execute("SELECT * FROM inventory ORDER BY stock_level ASC").fetchall()
        requests = conn.execute("SELECT * FROM part_requests ORDER BY requested_at DESC").fetchall()
        
        # Safely parse Postgres decimal values to Python floats
        clean_items = []
        for i in items:
            row = dict(i)
            row['unit_price'] = float(row['unit_price']) if row['unit_price'] else 0.0
            clean_items.append(row)
            
        return jsonify({"status": "success", "inventory": clean_items, "requests": [dict(r) for r in requests]}), 200
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500
    finally: conn.close()

@app.route('/api/inventory/request-action', methods=['POST'])
def handle_part_request():
    data = request.json
    req_id = data.get('request_id')
    action = data.get('action') 
    conn = get_db_connection()
    try:
        part_req = conn.execute("SELECT * FROM part_requests WHERE id = %s", (req_id,)).fetchone()
        if not part_req: return jsonify({"status": "error", "message": "Request not found"}), 404
        
        if action == 'Approve':
            conn.execute("UPDATE part_requests SET status = 'Approved' WHERE id = %s", (req_id,))
            conn.execute("UPDATE tickets SET status = 'In Progress', last_updated_at = NOW() WHERE ticket_id = %s", (part_req['ticket_id'],))
            
            conn.execute("UPDATE inventory SET stock_level = GREATEST(0, stock_level - 1) WHERE LOWER(item_name) LIKE LOWER(%s)", (f"%{part_req['part_name']}%",))
            
            add_notification(part_req['tech_name'], None, f"✅ APPROVED: Part '{part_req['part_name']}' is ready for pickup. Ticket {part_req['ticket_id']} resumed.", db_conn=conn)
        else:
            conn.execute("UPDATE part_requests SET status = 'Denied' WHERE id = %s", (req_id,))
            conn.execute("UPDATE tickets SET status = 'In Progress', last_updated_at = NOW() WHERE ticket_id = %s", (part_req['ticket_id'],))
            add_notification(part_req['tech_name'], None, f"❌ DENIED: Request for '{part_req['part_name']}' was rejected.", is_urgent=1, db_conn=conn)
            
        return jsonify({"status": "success"}), 200
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500
    finally: conn.close()

@app.route('/api/inventory/search-online', methods=['POST'])
def search_online():
    part_name = request.json.get('part_name', '').strip()

    if not part_name:
        return jsonify({"status": "error", "message": "part_name is required."}), 400
        
    try:
        task_client = AI_POOL.get("market_search")
        if task_client: 
            try:
                # 1. UPDATED PROMPT: Prioritizing GeM
                prompt = f"""Search the live internet for where to buy '{part_name}' in India right now. 
                Return EXACTLY a JSON array of 3 trusted Indian suppliers. You MUST prioritize the Government e-Marketplace (GeM) at gem.gov.in as one of your top choices. Other examples include Moglix, Amazon.in, or IndustryBuying. 
                Each object must contain: "vendor", "price", "delivery", and "url".
                CRITICAL RULE FOR URLS: Do not try to guess a specific product link. Instead, generate a 'General Search URL' for that website that searches for the part name. 
                Example for GeM: 'https://mkp.gem.gov.in/search?q={part_name.replace(' ', '+')}'
                Example for Amazon: 'https://www.amazon.in/s?k={part_name.replace(' ', '+')}'
                Example for Moglix: 'https://www.moglix.com/search?q={part_name.replace(' ', '+')}'
                Ensure the URLs are properly formatted for searching."""
                
                config = types.GenerateContentConfig(
                    tools=[{"google_search": {}}]
                )
                response = task_client.models.generate_content(
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
                    raise ValueError("AI failed to format data.")
                    
            except Exception as ai_error:
                print(f"AI Quota Exceeded or Formatting Error: {ai_error}")
                # 2. UPDATED FALLBACK: GeM is now the primary offline default
                clean_part = part_name.replace(' ', '+')
                fallback_suppliers = [
                    {"vendor": "Government e-Marketplace (GeM)", "price": "₹ Varies", "delivery": "Official Portal", "url": f"https://mkp.gem.gov.in/search?q={clean_part}"},
                    {"vendor": "IndustryBuying", "price": "₹ Varies", "delivery": "Check Site", "url": f"https://www.industrybuying.com/search/?q={clean_part}"},
                    {"vendor": "Moglix", "price": "₹ Varies", "delivery": "Check Site", "url": f"https://www.moglix.com/search?q={clean_part}"}
                ]
                return jsonify({"status": "success", "suppliers": fallback_suppliers})
                
        else:
            return jsonify({"status": "error", "message": "AI not configured."})
    except Exception as e:
        print("Fatal Search Error:", e)
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/inventory/buy', methods=['POST'])
def buy_part():
    part_name = request.json.get('part_name')
    conn = get_db_connection()
    try:
        conn.execute("UPDATE inventory SET stock_level = stock_level + 10 WHERE item_name = %s", (part_name,))
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
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)''', 
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
                user = conn.execute("SELECT email FROM users WHERE name = %s", (q['user_name'],)).fetchone()
                
                if user and user['email']:
                    subject = f"Did we fix it? QA Survey for {ticket_id}"
                    # Grabs your live domain from the .env file, or falls back to localhost for testing
                    base_url = os.getenv("FRONTEND_URL", "http://127.0.0.1:5005")
                    link_yes = f"{base_url}/api/qa/{ticket_id}?answer=yes"
                    link_no = f"{base_url}/api/qa/{ticket_id}?answer=no"
                    
                    body = f"Hi {q['user_name']},\n\nYour maintenance request {ticket_id} was marked as 'Resolved' by {q['assigned_technician']}.\n\nTo ensure quality, please let us know if the issue was completely fixed by clicking one of the links below:\n\nYES, IT IS FIXED: {link_yes}\n\nNO, STILL BROKEN: {link_no}\n\nThank you,\nInfraHub QA Bot"
                    
                    threading.Thread(target=send_async_email, args=(app, user['email'], subject, body)).start()
                    print(f"🤖 [QA AGENT] Sent quality check email to {q['user_name']} for {ticket_id}")
                
                conn.execute("UPDATE tickets SET qa_sent = 1 WHERE ticket_id = %s", (ticket_id,))
            
            techs = conn.execute("SELECT name, max_shift_hours, overtime_opt_in FROM technicians WHERE is_on_shift = 1 AND on_break = 0").fetchall()
            for t in techs:
                max_minutes = (t['max_shift_hours'] if t['max_shift_hours'] else 8) * 60
                
                minutes_worked_today = conn.execute('''
                    SELECT SUM(time_taken_mins) as total_mins FROM tickets 
                    WHERE assigned_technician = %s 
                    AND status IN ('Resolved', 'Closed') 
                    AND (last_updated_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Kolkata')::DATE = (NOW() AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Kolkata')::DATE
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
                conn.execute("UPDATE tickets SET assigned_technician = 'Unassigned', status = 'Pending' WHERE ticket_id = %s", (task['ticket_id'],))
                add_notification(None, 'Portal Admin', f"SYSTEM REBALANCE: {task['assigned_technician']} went off duty. {task['ticket_id']} returned to AI Dispatcher.", is_urgent=1, db_conn=conn)
                
            overdue = conn.execute("SELECT ticket_id FROM tickets WHERE priority IN ('Urgent', 'High') AND status = 'Pending' AND assigned_technician = 'Unassigned'").fetchall()
            for o in overdue:
                conn.execute("UPDATE tickets SET status = 'Escalated' WHERE ticket_id = %s", (o['ticket_id'],))
                add_notification(None, 'Portal Admin', f"ESCALATION: {o['ticket_id']} is critical and unassigned! Intervene.", is_urgent=1, db_conn=conn)
            
            unassigned = conn.execute("SELECT ticket_id, department, building FROM tickets WHERE assigned_technician = 'Unassigned' AND status IN ('Pending', 'Escalated')").fetchall()
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
        conn.execute('INSERT INTO tool_checkouts (tool_name, tech_name) VALUES (%s, %s)', 
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
        conn.execute("UPDATE tool_checkouts SET status = 'Returned' WHERE id = %s", (log_id,))
        return jsonify({"status": "success"})
    finally:
        conn.close()

@app.route('/api/analytics/budget')
def get_budget_analytics():
    try:
        departments = [
            "IT & Network Services", 
            "Electrical Maintenance", 
            "Plumbing Maintenance", 
            "Civil Maintenance", 
            "Air Conditioning & Ventilation Services",
            "Security & Surveillance",
            "Housekeeping Services",
            "Fire Safety Systems",
            "Water Supply & Sewage Management",
            "Equipment Support"
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
        
        task_client = AI_POOL.get("alternative")
        if task_client:
            prompt = f"A technician desperately needs '{part_name}' but it is completely out of stock. Based ONLY on this list of available inventory: [{avail_str}], what is the single best emergency alternative they can use? Keep your response under 15 words. Format: 'AI Suggests: [Item Name]'"
            response = task_client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
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
        item = conn.execute("SELECT unit_price FROM inventory WHERE item_name = %s", (item_name,)).fetchone()
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
        drafts = conn.execute("SELECT * FROM inventory WHERE stock_level <= reorder_threshold").fetchall()
        
        clean_drafts = []
        for d in drafts:
            row = dict(d)
            row['unit_price'] = float(row['unit_price']) if row['unit_price'] else 0.0
            clean_drafts.append(row)
            
        return jsonify({"status": "success", "data": clean_drafts})
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
        item = conn.execute("SELECT stock_level FROM inventory WHERE item_name = %s", (item_name,)).fetchone()
        if not item: return jsonify({"status": "error", "message": "Item not found"})
        
        new_stock = max(0, item['stock_level'] + delta)
        conn.execute("UPDATE inventory SET stock_level = %s WHERE item_name = %s", (new_stock, item_name))
        
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
           
        })
    except Exception as e:
        return jsonify({"status": "error"})
    finally:
        conn.close()


LAST_BRIEFING_TEXT = ""
LAST_BRIEFING_TIME = 0

@app.route('/api/ai/briefing')
def get_campus_briefing():
    global LAST_BRIEFING_TEXT, LAST_BRIEFING_TIME
    conn = get_db_connection()
    try:
        pending_count = conn.execute("SELECT COUNT(*) as c FROM tickets WHERE status = 'Pending'").fetchone()['c']
        active_count = conn.execute("SELECT COUNT(*) as c FROM tickets WHERE status IN ('Assigned', 'In Progress')").fetchone()['c']
        low_stock = conn.execute("SELECT COUNT(*) as c FROM inventory WHERE stock_level <= reorder_threshold").fetchone()['c']
        
        fallback_briefing = f"Campus health is stable. We currently have {pending_count} unassigned requests, {active_count} jobs actively being worked on, and {low_stock} stockroom items falling below safety thresholds."
        
        # INCREASED CACHE TO 5 MINUTES (300 SECONDS)
        if time.time() - LAST_BRIEFING_TIME < 300 and LAST_BRIEFING_TEXT:
            return jsonify({"status": "success", "briefing": LAST_BRIEFING_TEXT})

        try:
            task_client = AI_POOL.get("briefing")
            if task_client:
                prompt = f"You are the AI manager of a campus maintenance system. Write a quick, professional 2-sentence morning briefing. Current stats: {pending_count} pending tickets, {active_count} active tickets, and {low_stock} low stock items. Be concise, operational, and do not use formatting like bolding or asterisks."
                response = task_client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
                
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
            conn.execute("INSERT INTO leave_requests (tech_name, start_date, end_date, reason) VALUES (%s, %s, %s, %s)", 
                         (data['tech_name'], data['start_date'], data['end_date'], data['reason']))
            add_notification(None, 'Portal Admin', f"LEAVE REQUEST: {data['tech_name']} requested time off from {data['start_date']} to {data['end_date']}.", db_conn=conn)
            return jsonify({"status": "success"})
        else:
            tech_name = request.args.get('name')
            leaves = conn.execute("SELECT * FROM leave_requests WHERE tech_name = %s ORDER BY id DESC", (tech_name,)).fetchall()
            return jsonify({"status": "success", "data": [dict(l) for l in leaves]})
    finally: conn.close()

@app.route('/api/technicians/shift-trades', methods=['POST', 'GET'])
def handle_trades():
    conn = get_db_connection()
    try:
        if request.method == 'POST':
            data = request.json
            conn.execute("INSERT INTO shift_trades (requester, department, target_date) VALUES (%s, %s, %s)", 
                         (data['requester'], data['department'], data['target_date']))
            return jsonify({"status": "success"})
        else:
            dept = request.args.get('department')
            trades = conn.execute("SELECT * FROM shift_trades WHERE department = %s AND status = 'Pending' ORDER BY id DESC", (dept,)).fetchall()
            return jsonify({"status": "success", "data": [dict(t) for t in trades]})
    finally: conn.close()

@app.route('/api/technicians/timesheet/<tech_name>', methods=['GET'])
def download_timesheet(tech_name):
    conn = get_db_connection()
    try:
        tickets = conn.execute("SELECT ticket_id, issue, time_taken_mins, last_updated_at FROM tickets WHERE assigned_technician = %s AND status IN ('Resolved', 'Closed')", (tech_name,)).fetchall()
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
        feedbacks = conn.execute("SELECT ticket_id, user_name, issue, user_rating, user_feedback, last_updated_at FROM tickets WHERE assigned_technician = %s AND status = 'Closed' AND user_rating > 0 ORDER BY last_updated_at DESC LIMIT 5", (tech_name,)).fetchall()
        
        tech = conn.execute("SELECT badges_unlocked, points FROM technicians WHERE name = %s", (tech_name,)).fetchone()
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
        conn.execute("UPDATE users SET account_status = 'Approved' WHERE email = %s", (email,))
        conn.execute("UPDATE technicians SET account_status = 'Approved' WHERE name = (SELECT name FROM users WHERE email = %s)", (email,))
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error"})
    finally: conn.close()

@app.route('/api/admin/process-leave', methods=['POST'])
def process_leave():
    req_id = request.json.get('id')
    decision = request.json.get('status') 
    conn = get_db_connection()
    try:
        conn.execute("UPDATE leave_requests SET status = %s WHERE id = %s", (decision, req_id))
        leave = conn.execute("SELECT tech_name FROM leave_requests WHERE id = %s", (req_id,)).fetchone()
        add_notification(leave['tech_name'], None, f"HR Department Update: Your leave request has been officially {decision.upper()}.", db_conn=conn)
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error"})
    finally: conn.close()

# ==========================================
# RESTORED AI & FIELD OPS ROUTES
# ==========================================
@app.route('/api/ai/preflight', methods=['POST'])
def ai_preflight():
    issue = request.json.get('issue')
    conn = get_db_connection()
    try:
        inv = conn.execute("SELECT item_name, stock_level FROM inventory").fetchall()
        stock_list = ", ".join([f"{i['item_name']} (Qty: {i['stock_level']})" for i in inv])
        
        task_client = AI_POOL.get("classify") # Using the classify key here is fine, or create a specific one
        if task_client:
            try:
                prompt = f"A technician is about to accept this issue: '{issue}'. Look at our live inventory: [{stock_list}]. Identify 1 or 2 parts they will likely need. Format reply as exactly: 'Requires: [parts]. Status: [In Stock / OUT OF STOCK]'."
                resp = task_client.models.generate_content(model='gemini-2.5-flash', contents=prompt).text.strip()
                return jsonify({"status": "success", "analysis": resp})
            except Exception as e:
                return jsonify({"status": "success", "analysis": "Requires: Standard Toolkit. Status: IN STOCK (AI Fallback)"})
                
        return jsonify({"status": "error", "message": "AI offline."})
    finally: conn.close()

@app.route('/api/ai/summarize', methods=['POST'])
def ai_summarize():
    issue = request.json.get('issue')
    task_client = AI_POOL.get("summarize")
    if task_client:
        try:
            prompt = f"Summarize this maintenance issue into a quick, 1-sentence TL;DR for a busy technician: {issue}"
            resp = task_client.models.generate_content(model='gemini-2.5-flash', contents=prompt).text.strip()
            return jsonify({"status": "success", "summary": resp})
        except Exception as e:
             return jsonify({"status": "success", "summary": f"System Note: {issue[:60]}... (AI Quota Exhausted)"})
             
    return jsonify({"status": "error", "message": "AI offline."})

@app.route('/api/ai/draft-update', methods=['POST'])
def ai_draft_update():
    data = request.get_json()
    issue = data.get('issue')
    status = data.get('status')
    user_name = data.get('user_name', 'Campus Staff')
    tech_name = data.get('tech_name', 'Campus Technician')
    
    task_client = AI_POOL.get("email_draft")
    if task_client:
        try:
            prompt = f"Write a formal, polite email update to {user_name} regarding their reported issue: '{issue}'. Current Status is: '{status}'. Sign the email from {tech_name}."
            resp = task_client.models.generate_content(model='gemini-2.5-flash', contents=prompt).text.strip()
            return jsonify({"status": "success", "draft": resp})
        except Exception as e:
            fallback = f"Dear {user_name},\n\nThis is an automated update regarding your maintenance request. Its current status is now: {status}.\n\nBest regards,\n{tech_name}"
            return jsonify({"status": "success", "draft": fallback})
            
    return jsonify({"status": "error", "message": "AI offline."})

@app.route('/api/tickets/<ticket_id>/update-status', methods=['PUT'])
def update_ticket_status(ticket_id):
    new_status = request.json.get('status')
    conn = get_db_connection()
    try:
        conn.execute("UPDATE tickets SET status = %s, last_updated_at = NOW() WHERE ticket_id = %s", (new_status, ticket_id))
        return jsonify({"status": "success"})
    finally: conn.close()

@app.route('/api/technicians/toggle-break', methods=['PUT'])
def toggle_break():
    tech_name = request.json.get('name')
    conn = get_db_connection()
    try:
        tech = conn.execute("SELECT on_break FROM technicians WHERE name = %s", (tech_name,)).fetchone()
        new_status = 0 if tech['on_break'] == 1 else 1
        conn.execute("UPDATE technicians SET on_break = %s WHERE name = %s", (new_status, tech_name))
        return jsonify({"status": "success"})
    finally: conn.close()

@app.route('/api/technicians/toggle-overtime', methods=['PUT'])
def toggle_overtime():
    tech_name = request.json.get('name')
    conn = get_db_connection()
    try:
        tech = conn.execute("SELECT overtime_opt_in FROM technicians WHERE name = %s", (tech_name,)).fetchone()
        new_status = 0 if tech['overtime_opt_in'] == 1 else 1
        conn.execute("UPDATE technicians SET overtime_opt_in = %s WHERE name = %s", (new_status, tech_name))
        return jsonify({"status": "success"})
    finally: conn.close()

@app.route('/api/technicians/location', methods=['PUT'])
def update_location():
    data = request.json
    conn = get_db_connection()
    try:
        conn.execute("UPDATE technicians SET current_building = %s WHERE name = %s", (data['building'], data['name']))
        return jsonify({"status": "success"})
    finally: conn.close()



@app.route('/api/tickets/hazard', methods=['POST'])
def report_hazard():
    data = request.json
    desc = data.get('description')
    tech_name = data.get('name')
    bldg = data.get('building', 'Unknown')
    conn = get_db_connection()
    try:
        prompt = f"A field technician quickly typed this hazard report: '{desc}'. Expand this into a formal, professional 2-sentence maintenance ticket issue."
        
        task_client = AI_POOL.get("classify")
        if task_client:
            try:
                resp = task_client.models.generate_content(model='gemini-2.5-flash', contents=prompt).text.strip()
            except Exception:
                resp = f"Hazard reported by {tech_name}: {desc}"
        else:
            resp = f"Hazard reported by {tech_name}: {desc}"
        
        ai_decision = classify_ticket_with_ai(resp)
        ticket_id = 'HAZ-' + str(random.randint(1000,9999))
        
        conn.execute('INSERT INTO tickets (ticket_id, user_name, role, department, building, location, issue, priority, ai_analysis) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)',
            (ticket_id, tech_name, 'Campus Technician', ai_decision['department'], bldg, 'Reported via Snap&Report', resp, ai_decision['priority'], ai_decision['ai_analysis']))
        
        tech = tool_get_available_technician(ai_decision['department'], bldg, db_conn=conn)
        if tech['status'] == 'success':
            tool_assign_ticket(ticket_id, tech['technician_name'], db_conn=conn)
        
        return jsonify({"status": "success", "ticket": resp})
    finally: conn.close()


# ==========================================
# 5. WHATSAPP AI BOT INTEGRATION (TWILIO)
# ==========================================
# ==========================================
# SECURITY LAYER: VALIDATE TWILIO SIGNATURE
# ==========================================
def validate_twilio_request(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Validate that the request actually came from Twilio
        validator = RequestValidator(os.getenv("TWILIO_AUTH_TOKEN"))
        signature = request.headers.get('X-Twilio-Signature', '')
        if validator.validate(request.url, request.form, signature):
            return f(*args, **kwargs)
        else:
            return "Forbidden", 403
    return decorated_function

# ==========================================
@app.route('/api/whatsapp', methods=['POST'])
@validate_twilio_request
def whatsapp_bot():
    incoming_msg = request.values.get('Body', '').strip()
    sender_number = request.values.get('From', '').replace('whatsapp:', '')

    resp = MessagingResponse()
    msg = resp.message()
    conn = get_db_connection()

    try:
        # Fetch or Create Session
        session = conn.execute("SELECT * FROM whatsapp_sessions WHERE phone_number = %s", (sender_number,)).fetchone()
        
        if not session:
            conn.execute("INSERT INTO whatsapp_sessions (phone_number) VALUES (%s)", (sender_number,))
            session = {'current_state': 'IDLE'}
        
        state = session['current_state']

        if state == 'IDLE':
            if len(incoming_msg) > 10: 
                conn.execute("UPDATE whatsapp_sessions SET current_state = 'AWAITING_BUILDING', temp_issue = %s, last_interaction = NOW() WHERE phone_number = %s", (incoming_msg, sender_number))
                msg.body(f"Got it. You reported: '{incoming_msg}'.\n\nWhere is this issue located? Please select a building:\n1. Main Building\n2. Library Building\n3. Building A\n4. Building B\n5. Building C")
            else:
                msg.body("Hi there! To raise a maintenance request, please describe the issue in detail (more than 10 characters).")
                
        elif state == 'AWAITING_BUILDING':
            buildings = {"1": "Main Building", "2": "Library Building", "3": "Building A", "4": "Building B", "5": "Building C"}
            if incoming_msg in buildings:
                selected_building = buildings[incoming_msg]
                conn.execute("UPDATE whatsapp_sessions SET current_state = 'AWAITING_LOCATION', temp_building = %s, last_interaction = NOW() WHERE phone_number = %s", (selected_building, sender_number))
                msg.body(f"Selected: {selected_building}.\n\nPlease reply with the specific floor and room number (e.g., '2nd Floor, Room 204').")
            else:
                msg.body("Invalid selection. Please reply with the number corresponding to the building (1-5).")

        elif state == 'AWAITING_LOCATION':
            final_location = incoming_msg
            issue = session['temp_issue']
            building = session['temp_building']
            
            # 1. Run AI Classification
            ai_decision = classify_ticket_with_ai(issue)
            dept = ai_decision.get("department", "Civil Maintenance")
            pri = ai_decision.get("priority", "Medium")
            analysis = ai_decision.get("ai_analysis", "Routed via WhatsApp Bot")
            
            # 2. Create Ticket
            ticket_id = f"REQ-{random.randint(1000, 9999)}"
            
            conn.execute("""
    INSERT INTO tickets 
    (ticket_id, user_name, contact_number, role, department, building, location, issue, priority, status, assigned_technician, ai_analysis)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
""", (
    ticket_id, 
    f"WhatsApp User ({sender_number[-4:]})", 
    sender_number, # The newly saved number
    "Campus User", 
    dept, 
    building, 
    final_location, 
    issue, 
    pri, 
    "Pending", 
    "Unassigned",
    analysis
))

            # ---> NEW: IMMEDIATELY TRIGGER AI DISPATCHER <---
            tech = tool_get_available_technician(dept, building, db_conn=conn)
            if tech['status'] == 'success':
                assigned_name = tech['technician_name']
                tool_assign_ticket(ticket_id, assigned_name, db_conn=conn)
                
                # Notify the assigned Tech on the web portal
                add_notification(assigned_name, None, f"AI DISPATCH: {ticket_id} (WhatsApp) routed to your queue.", is_urgent=1 if pri == 'Urgent' else 0, db_conn=conn)
                
                # Send WhatsApp confirmation with the Tech's name
                msg.body(f"✅ *Ticket Created: {ticket_id}*\nAssigned Dept: {dept}\nPriority: {pri}\nLocation: {building}, {final_location}\n\nGood news! We have automatically routed this directly to *{assigned_name}*. You will receive updates here as the status changes.")
            else:
                # If no tech is on shift, leave it unassigned and alert the Admin
                add_notification(None, 'Portal Admin', f"WARNING: WhatsApp ticket {ticket_id} could not be assigned. No techs in {dept}!", is_urgent=1, db_conn=conn)
                msg.body(f"✅ *Ticket Created: {ticket_id}*\nAssigned Dept: {dept}\nPriority: {pri}\nLocation: {building}, {final_location}\n\nYour request is in our system. A technician will be assigned shortly.")

            # Reset the session back to IDLE
            conn.execute("UPDATE whatsapp_sessions SET current_state = 'IDLE', temp_issue = NULL, temp_building = NULL WHERE phone_number = %s", (sender_number,))

    except Exception as e:
        print(f"WhatsApp Workflow Error: {e}")
        msg.body("I encountered an error processing that request. Please try again.")
    finally:
        conn.close()

    return str(resp)
def send_whatsapp_update(user_phone, ticket_id, new_status, additional_info=""):
    """Pushes a proactive update via Twilio."""
    if not twilio_client: return

    # Ensure phone number is formatted for Twilio Sandbox
    formatted_phone = f"whatsapp:+{user_phone}" if not str(user_phone).startswith("whatsapp:") else user_phone
    
    message_body = f"🔔 *Update on {ticket_id}*\n\nStatus: *{new_status}*"
    
    if new_status == "In Progress":
        message_body += f"\n\nA technician has started working on this."
    elif new_status == "Resolved":
        message_body += f"\n\nThis issue has been marked as resolved. {additional_info}"

    try:
        twilio_client.messages.create(
            from_=TWILIO_PHONE_NUMBER,
            body=message_body,
            to=formatted_phone
        )
    except Exception as e:
        print(f"Failed to send Twilio update: {e}")

init_db()
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

if __name__ == '__main__':
    # Start the monitoring agent ONLY when running the main app, 
    # preventing duplicates in multi-worker production environments.
    monitor_thread = threading.Thread(target=monitoring_agent_loop, daemon=True)
    monitor_thread.start()

    app.run(debug=True, port=5005, use_reloader=False)
