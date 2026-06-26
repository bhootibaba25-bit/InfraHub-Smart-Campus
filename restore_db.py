import pandas as pd
import sqlite3
import os

def restore_database():
    excel_file = 'Campus_Database.xlsx'
    db_file = 'campus_hub.db'

    if not os.path.exists(excel_file):
        print(f"❌ Error: Could not find '{excel_file}'.")
        return

    conn = sqlite3.connect(db_file)
    c = conn.cursor()

    users_added = 0
    techs_added = 0

    # ==========================================
    # 1. IMPORT "Users" TAB
    # ==========================================
    print("⏳ Loading 'Users' sheet...")
    try:
        df_users = pd.read_excel(excel_file, sheet_name='Users')
        df_users = df_users.fillna('') # Replace NaN with empty strings

        for index, row in df_users.iterrows():
            custom_id = str(row.get('User_ID', ''))
            role = str(row.get('User_role', 'Campus User'))
            name = str(row.get('Name', ''))
            mobile = str(row.get('Mobile_No', ''))
            email = str(row.get('Email_Address', ''))
            password = str(row.get('Password', 'User@1234'))

            if email: # Only insert if they have an email
                try:
                    c.execute('''INSERT INTO users (custom_id, name, email, password, role, mobile_no) 
                                 VALUES (?, ?, ?, ?, ?, ?)''', 
                                 (custom_id, name, email, password, role, mobile))
                    users_added += 1
                except sqlite3.IntegrityError:
                    pass # Skip if email already exists
    except Exception as e:
        print(f"⚠️ Error reading Users sheet: {e}")

    # ==========================================
    # 2. IMPORT "Technician" TAB
    # ==========================================
    print("⏳ Loading 'Technician' sheet...")
    try:
        df_techs = pd.read_excel(excel_file, sheet_name='Technician')
        df_techs = df_techs.fillna('')

        for index, row in df_techs.iterrows():
            custom_id = str(row.get('Technician_ID', ''))
            name = str(row.get('Name', ''))
            mobile = str(row.get('Mobile_No', ''))
            email = str(row.get('Email address', '')) # Notice the exact spelling from your screenshot
            password = str(row.get('password', 'Tech@1234')) # Notice lowercase 'p' from your screenshot
            department = str(row.get('Department', 'Pending Assignment'))
            role = 'Campus Technician'

            if email:
                # A. Add to the Users table so the technician can log in
                try:
                    c.execute('''INSERT INTO users (custom_id, name, email, password, role, mobile_no) 
                                 VALUES (?, ?, ?, ?, ?, ?)''', 
                                 (custom_id, name, email, password, role, mobile))
                except sqlite3.IntegrityError:
                    pass # Skip if login already exists
                
                # B. Add to the Technicians table for AI Ticket Assignment
                try:
                    existing_tech = c.execute("SELECT name FROM technicians WHERE name = ?", (name,)).fetchone()
                    if not existing_tech:
                        c.execute('''INSERT INTO technicians 
                                 (custom_id, name, department, current_active_hours, max_shift_hours, is_on_shift, mobile_no, points) 
                                 VALUES (?, ?, ?, 0, 8, 1, ?, 0)''', 
                                 (custom_id, name, department, mobile))
                        techs_added += 1
                except Exception as e:
                    print(f"Error adding tech {name}: {e}")
    except Exception as e:
        print(f"⚠️ Error reading Technician sheet: {e}")

    # Save and close
    conn.commit()
    conn.close()
    
    print("\n==========================================")
    print(f"✅ SUCCESS! Database restored.")
    print(f"👥 Added {users_added} regular users.")
    print(f"🛠️ Added {techs_added} technicians.")
    print("==========================================")

if __name__ == "__main__":
    restore_database()