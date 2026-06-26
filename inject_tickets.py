import sqlite3
import random

def inject_historical_tickets():
    conn = sqlite3.connect('campus_hub.db')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # ---> THIS LINE WIPES OUT THE OLD SYSTEM AI TICKETS <---
    print("🧹 Clearing old system AI requests from the database...")
    c.execute("DELETE FROM tickets") 
    
    # Optional: If you also want to reset technician daily workloads back to 0 for a clean test:
    c.execute("UPDATE technicians SET current_active_hours = 0")

    # Fetch all real users from your database
    users = c.execute("SELECT name FROM users WHERE role IN ('Campus User', 'Student', 'Faculty', 'Administrative Staff', 'Facility Manager')").fetchall()
    user_names = [u['name'] for u in users]

    if not user_names:
        print("❌ Error: Could not find any normal users in the database to assign these tickets to!")
        return

    tickets = [
        ("IT & Network Services", "Main Building", "Room 101", "The projector in Room 101 isn't connecting to my laptop."),
        ("IT & Network Services", "Library Building", "Study Hall 2", "Wi-Fi is completely dead in the library study hall."),
        ("IT & Network Services", "Building A", "Faculty Office 302", "Need to reset my faculty portal password, it's locked out."),
        ("IT & Network Services", "Building C", "2nd Floor Hallway", "The printer on the 2nd floor is jamming paper every time."),
        ("IT & Network Services", "Building D1", "Lab B", "Desktop in Lab B won't boot up, just shows a black screen."),
        ("IT & Network Services", "Building B", "Dorm Room 405", "Can't connect to the campus VPN from the dorms."),
        ("IT & Network Services", "Building C", "Lecture Hall 3", "The smartboard touch function in Lecture Hall 3 is uncalibrated."),
        ("IT & Network Services", "Building D2", "Engineering Lab", "Software update required for the engineering lab computers."),
        ("IT & Network Services", "Library Building", "Desk 12", "Ethernet port at desk 12 is physically broken."),
        ("IT & Network Services", "Main Building", "Office 104", "Email client on my office PC keeps crashing."),
        ("IT & Network Services", "Building A", "Conference Room B", "Need a replacement HDMI cable for the conference room."),
        ("IT & Network Services", "Main Building", "Auditorium", "Audio system in the main auditorium has a loud buzzing noise."),
        ("IT & Network Services", "Library Building", "Workstation 5", "Mouse is missing from workstation 5 in the public lab."),
        ("IT & Network Services", "Building C", "Office 210", "Can you install SPSS on my department laptop."),
        ("IT & Network Services", "Building D1", "Research Wing", "Server access is denied for the new research assistants."),
        ("Electrical Maintenance", "Building A", "Main Hallway", "Half the lights in the main hallway are flickering."),
        ("Electrical Maintenance", "Building B", "Dorm Room 202", "Power outlet near the window in my dorm room sparked when I plugged in a lamp."),
        ("Electrical Maintenance", "Building C", "North Stairwell", "The exit sign at the north stairwell is not lighting up."),
        ("Electrical Maintenance", "Main Building", "Staff Room", "Circuit breaker keeps tripping when we run the microwave in the staff room."),
        ("Electrical Maintenance", "Building C", "Exterior Pathway", "The street lamp outside Building C is completely dark."),
        ("Electrical Maintenance", "Building D1", "Basement", "Humming noise coming from the electrical panel in the basement."),
        ("Electrical Maintenance", "Main Building", "Office 402", "Need a new lightbulb for the desk lamp in Office 402."),
        ("Electrical Maintenance", "Main Building", "Front Entrance", "The automatic sliding doors at the main entrance have no power."),
        ("Electrical Maintenance", "Building A", "Mens Restroom 1st Fl", "Exhaust fan in the men's restroom won't turn on."),
        ("Electrical Maintenance", "Building C", "Classroom 2B", "Exposed wires found near the baseboard in classroom 2B."),
        ("Electrical Maintenance", "Library Building", "Reading Room", "Emergency lights in the library didn't work during the drill."),
        ("Electrical Maintenance", "Building A", "Lecture Room 4", "The projector screen motor is dead, it's stuck halfway down."),
        ("Electrical Maintenance", "Building D2", "Elevator 2", "Elevator 2 buttons aren't lighting up when pressed."),
        ("Electrical Maintenance", "Library Building", "Computer Lab", "No power to any of the outlets on the right side of the computer lab."),
        ("Electrical Maintenance", "Main Building", "Cafeteria Kitchen", "The main breaker switch in the cafeteria kitchen is stuck."),
        ("Plumbing Maintenance", "Building B", "Womens Restroom 2nd Fl", "The sink in the women's restroom on floor 2 is leaking underneath."),
        ("Plumbing Maintenance", "Main Building", "Restroom Stall 3", "Toilet in stall 3 is completely clogged and overflowing."),
        ("Plumbing Maintenance", "Building D1", "Locker Room", "No hot water in the faculty locker room showers."),
        ("Plumbing Maintenance", "Building D1", "Gym Hallway", "Water fountain outside the gym has very low water pressure."),
        ("Plumbing Maintenance", "Main Building", "Cafeteria Exterior", "Strong sewage smell coming from the drains near the cafeteria."),
        ("Plumbing Maintenance", "Building C", "Basement Level", "The main water pipe in the basement has a slow drip."),
        ("Plumbing Maintenance", "Main Building", "Kitchen", "Need the grease trap cleaned in the main kitchen."),
        ("Plumbing Maintenance", "Building A", "Mens Restroom 3rd Fl", "The automatic flush sensor on the urinal is broken."),
        ("Plumbing Maintenance", "Building B", "Dorm Bathroom 310", "Water is pooling around the base of the toilet in the dorm bathroom."),
        ("Plumbing Maintenance", "Main Building", "Front Lawn", "The outdoor sprinkler head near the main sign is broken and shooting water up."),
        ("Plumbing Maintenance", "Building D2", "Chemistry Lab 1", "Faucet handles in the chemistry lab are stuck tight."),
        ("Plumbing Maintenance", "Building D1", "Utility Closet", "Water heater in Building D seems to be making loud clanking noises."),
        ("Plumbing Maintenance", "Building A", "Janitor Closet", "The drain in the janitor's closet is backing up."),
        ("Plumbing Maintenance", "Main Building", "Breakroom 2", "Pipe under the coffee machine in the breakroom burst."),
        ("Plumbing Maintenance", "Building C", "3rd Floor Restroom", "Loud banging in the pipes when you turn off the sink on the 3rd floor."),
        ("Civil Maintenance", "Building C", "South Staircase", "The handrail on the south staircase feels very loose."),
        ("Civil Maintenance", "Building D2", "Physics Lab Hallway", "Ceiling tile fell down in the hallway outside the physics lab."),
        ("Civil Maintenance", "Main Building", "Visitor Parking", "Large pothole in the visitor parking lot needs to be filled."),
        ("Civil Maintenance", "Building A", "Room 304", "The paint is peeling badly in the corner of Room 304."),
        ("Civil Maintenance", "Building B", "Dorm Room 112", "Window in my dorm won't close all the way, there's a draft."),
        ("Civil Maintenance", "Building C", "Storage Room A", "The door to the storage room is warped and scraping the floor."),
        ("Civil Maintenance", "Main Building", "Lobby", "Several floor tiles are cracked near the main entrance."),
        ("Civil Maintenance", "Building A", "Music Room", "The acoustic paneling in the music room is coming off the wall."),
        ("Civil Maintenance", "Library Building", "Exterior Pathway", "Brick pathway leading to the library is uneven and a tripping hazard."),
        ("Civil Maintenance", "Building D1", "Fire Door 2", "The hinge on the heavy fire door is completely snapped."),
        ("Civil Maintenance", "Main Building", "Staff Lounge", "Water stain growing on the ceiling in the staff lounge."),
        ("Civil Maintenance", "Building C", "Seminar Room 2", "Need a whiteboard securely mounted in the new seminar room."),
        ("Civil Maintenance", "Library Building", "Restroom Stall 1", "The locking mechanism on the bathroom stall is broken."),
        ("Civil Maintenance", "Building A", "Hallway Water Cooler", "Baseboard is detached near the water cooler."),
        ("Civil Maintenance", "Building D1", "Athletic Showers", "Grout needs replacing in the showers of the athletic center."),
        ("Air Conditioning & Ventilation Services", "Building C", "Lecture Hall A", "The AC in Lecture Hall A is blowing hot air."),
        ("Air Conditioning & Ventilation Services", "Main Building", "Office 201", "It is freezing in Office 201, the thermostat won't adjust."),
        ("Air Conditioning & Ventilation Services", "Library Building", "Reading Section", "Loud rattling noise coming from the AC vent in the library."),
        ("Air Conditioning & Ventilation Services", "Building D2", "Chemistry Lab 3", "The exhaust fan in the chemistry lab smells like burning dust."),
        ("Air Conditioning & Ventilation Services", "Building A", "1st Floor Hallway", "Water is dripping from the ceiling AC unit in the hallway."),
        ("Air Conditioning & Ventilation Services", "Building D1", "Server Room", "Need the air filters changed in the server room, it's getting dusty."),
        ("Air Conditioning & Ventilation Services", "Building B", "West Wing Dorms", "The heating isn't turning on at all in the west wing."),
        ("Air Conditioning & Ventilation Services", "Main Building", "Cafeteria", "Strong mildew smell coming through the vents in the cafeteria."),
        ("Air Conditioning & Ventilation Services", "Building A", "Room 105", "The thermostat in Room 105 has a blank screen."),
        ("Air Conditioning & Ventilation Services", "Main Building", "Auditorium Back Row", "Airflow is incredibly weak in the back row of the auditorium."),
        ("Air Conditioning & Ventilation Services", "Building C", "Exterior Roof", "The outdoor condenser unit is making a grinding noise."),
        ("Air Conditioning & Ventilation Services", "Library Building", "Rare Books Archive", "Humidity is too high in the rare books archive room."),
        ("Air Conditioning & Ventilation Services", "Building D1", "Gymnasium", "The vent cover fell off the ceiling in the gym."),
        ("Air Conditioning & Ventilation Services", "Building A", "Room 402", "The AC turns off randomly every 15 minutes."),
        ("Air Conditioning & Ventilation Services", "Main Building", "Roof", "Need a routine inspection of the rooftop chiller unit."),
        ("Security & Surveillance", "Building D1", "Research Wing Entrance", "My ID badge isn't swiping to let me into the research wing."),
        ("Security & Surveillance", "Library Building", "Exterior East", "The security camera outside the library seems to be pointing at the ground."),
        ("Security & Surveillance", "Building C", "Back Door", "The magnetic lock on the back door of Building C isn't engaging."),
        ("Security & Surveillance", "Main Building", "Lobby Panel", "The fire alarm panel in the lobby is beeping with a fault code."),
        ("Security & Surveillance", "Main Building", "Admin Desk", "Need a temporary access card for a visiting lecturer."),
        ("Security & Surveillance", "Building A", "Parking Garage Level 1", "The motion sensor light in the parking garage isn't triggering."),
        ("Security & Surveillance", "Main Building", "Front Glass Door", "The glass break sensor on the main door is hanging by a wire."),
        ("Security & Surveillance", "Building D1", "Server Room Door", "Keypad on the server room door has sticky buttons."),
        ("Security & Surveillance", "Building D2", "Bio Lab 1", "The emergency panic button in the lab was accidentally bumped."),
        ("Security & Surveillance", "Main Building", "Security Office", "Need to review footage from the cafeteria from yesterday at 2 PM."),
        ("Security & Surveillance", "Building D1", "Gym Entrance", "The turnstile at the gym entrance is jammed."),
        ("Security & Surveillance", "Main Building", "Parking Lot B", "Security gate arm in parking lot B won't go up."),
        ("Security & Surveillance", "Building A", "Office 315", "The lock cylinder on my office door is loose."),
        ("Security & Surveillance", "Main Building", "Bursar Office", "Need the safe in the bursar's office reset."),
        ("Security & Surveillance", "Main Building", "Front Desk", "The CCTV monitor at the front desk has gone black."),
        ("Housekeeping Services", "Main Building", "Main Lobby", "Major coffee spill in the main lobby that needs mopping."),
        ("Housekeeping Services", "Building C", "Student Union", "The trash cans in the student union are completely overflowing."),
        ("Housekeeping Services", "Building A", "2nd Floor Restrooms", "Out of paper towels and soap in the 2nd-floor restrooms."),
        ("Housekeeping Services", "Main Building", "Stairwell B", "Someone threw up in the stairwell near the cafeteria."),
        ("Housekeeping Services", "Library Building", "Main Floor", "The carpets in the library need a deep clean, they are very stained."),
        ("Housekeeping Services", "Building B", "1st Floor Restroom", "Graffiti needs to be removed from the bathroom stall."),
        ("Housekeeping Services", "Building D2", "Biology Lab 2", "Need a biohazard cleanup kit for a spill in the biology lab."),
        ("Housekeeping Services", "Main Building", "Atrium", "The windows in the main atrium are covered in fingerprints."),
        ("Housekeeping Services", "Main Building", "Faculty Lounge", "Dusting needed on the high shelves in the faculty lounge."),
        ("Housekeeping Services", "Building D1", "Gym Floor", "The floor in the gym is very sticky after yesterday's event."),
        ("Housekeeping Services", "Building C", "Break Area", "Recycling bins haven't been emptied in three days."),
        ("Housekeeping Services", "Building A", "Math Department", "Need the whiteboards thoroughly cleaned in all math classrooms."),
        ("Housekeeping Services", "Building C", "Side Entrance", "Mud tracked all down the hallway from the side entrance."),
        ("Housekeeping Services", "Main Building", "Auditorium", "The chairs in the auditorium need to be wiped down."),
        ("Housekeeping Services", "Library Building", "Indoor Garden", "Dead plant leaves need to be swept up from the indoor garden."),
        ("Fire Safety Systems", "Main Building", "Kitchen", "The fire extinguisher in the kitchen is missing its safety pin."),
        ("Fire Safety Systems", "Building A", "All Floors", "Need a routine pressure check on all extinguishers in Building A."),
        ("Fire Safety Systems", "Building B", "Dorm 214", "The smoke detector in my dorm is chirping (low battery)."),
        ("Fire Safety Systems", "Building D2", "Chemistry Lab", "The fire blanket box in the chemistry lab is stuck closed."),
        ("Fire Safety Systems", "Building C", "3rd Floor Hallway", "Fire hose reel on the 3rd floor looks damaged."),
        ("Fire Safety Systems", "Building A", "Stairwell North", "The emergency exit map is missing from the wall near the stairs."),
        ("Fire Safety Systems", "Library Building", "Front Entrance", "The glass on the manual fire alarm pull station is cracked."),
        ("Fire Safety Systems", "Building D1", "Server Room", "Need to test the fire suppression system in the server room."),
        ("Fire Safety Systems", "Building C", "Stairwell C", "The fire door to the stairwell is propped open and won't close automatically."),
        ("Fire Safety Systems", "Main Building", "Cafeteria Kitchen", "Inspection required for the kitchen hood fire suppression system."),
        ("Fire Safety Systems", "Building B", "Dorm 108", "The strobe light on the fire alarm in the deaf student dorm isn't flashing."),
        ("Fire Safety Systems", "Main Building", "Basement Valve", "The pressure gauge on the main sprinkler valve is in the red."),
        ("Fire Safety Systems", "Building D1", "New Annex", "Need new fire safety signage installed in the new annex."),
        ("Fire Safety Systems", "Building A", "Room 101", "The protective cover on the sprinkler head in Room 101 is missing."),
        ("Fire Safety Systems", "Main Building", "Campus Wide", "Schedule the annual fire safety drill for next Tuesday."),
        ("Water Supply & Sewage Management", "Building A", "1st Floor Fountain", "The water from the drinking fountain tastes metallic."),
        ("Water Supply & Sewage Management", "Building C", "4th Floor", "Total water pressure loss across the entire 4th floor."),
        ("Water Supply & Sewage Management", "Main Building", "Basement", "The sewage lift pump in the basement is making a weird noise."),
        ("Water Supply & Sewage Management", "Main Building", "Cafeteria Kitchen", "Need a water quality test done for the cafeteria supply line."),
        ("Water Supply & Sewage Management", "Building D1", "Utility Room", "The main water shutoff valve is heavily rusted and hard to turn."),
        ("Water Supply & Sewage Management", "Main Building", "Front Parking Lot", "There is a massive puddle forming near the outdoor storm drain."),
        ("Water Supply & Sewage Management", "Building A", "Boiler Room", "The backflow preventer valve needs its annual inspection."),
        ("Water Supply & Sewage Management", "Building B", "Utility Closet", "The water softener for the dorms isn't using any salt."),
        ("Water Supply & Sewage Management", "Building C", "Rear Parking Lot", "The manhole cover in the parking lot is loose and rattling."),
        ("Water Supply & Sewage Management", "Building D1", "Exterior Rear", "Sewer line backup causing flooding behind the gym."),
        ("Water Supply & Sewage Management", "Main Building", "Campus Grounds", "Need the catch basins cleaned out before the rainy season."),
        ("Water Supply & Sewage Management", "Building D2", "Lab 4", "The UV water purifier in the lab has a burnt-out bulb."),
        ("Water Supply & Sewage Management", "Building A", "Exterior Wall", "Water meter seems to be spinning rapidly even when nothing is on."),
        ("Water Supply & Sewage Management", "Building B", "Exterior Drain", "The main drainage pipe outside Building B is cracked."),
        ("Water Supply & Sewage Management", "Main Building", "Campus Network", "Routine flushing of the dead-end water mains required."),
        ("Equipment Support", "Building D2", "Bio Lab 2", "The centrifuge in Bio Lab 2 is vibrating violently when running."),
        ("Equipment Support", "Main Building", "Culinary Kitchen", "The industrial mixer in the culinary kitchen won't change speeds."),
        ("Equipment Support", "Building D1", "Gym Floor", "The treadmill nearest the window in the gym has a torn belt."),
        ("Equipment Support", "Building D2", "Science Lab 4", "Microscope #4 has a scratched objective lens."),
        ("Equipment Support", "Building C", "Maker Space", "The 3D printer in the maker space is clogging the filament."),
        ("Equipment Support", "Building D2", "Prep Room", "The autoclave is not reaching sterilization temperature."),
        ("Equipment Support", "Building D1", "Gym Floor", "The rowing machine's digital display is broken."),
        ("Equipment Support", "Building C", "Maker Space", "The laser cutter bed is no longer level."),
        ("Equipment Support", "Building D2", "Chemistry Lab", "The electronic analytical balance won't zero out."),
        ("Equipment Support", "Library Building", "Print Room", "Need the blades sharpened on the industrial paper cutter."),
        ("Equipment Support", "Building D2", "Physics Lab", "The oscilloscope light source has burned out."),
        ("Equipment Support", "Building A", "Art Studio", "The pottery kiln is displaying an error code E-04."),
        ("Equipment Support", "Building C", "Engineering Shop", "The CNC router is losing connection to the control PC."),
        ("Equipment Support", "Building D2", "Advanced Lab", "The mass spectrometer needs routine calibration."),
        ("Equipment Support", "Building D1", "Gymnasium", "The digital scoreboard in the gym has a dead pixel row.")
    ]

    print(f"Injecting {len(tickets)} fresh tickets requested by your 100 users...")
    
    count = 0
    for dept, bldg, loc, issue in tickets:
        random_user = random.choice(user_names)
        priority = 'Medium'
        if any(w in issue.lower() for w in ['burst', 'fire', 'leak', 'flood', 'blackout', 'sewage']): priority = 'Urgent'
        elif any(w in issue.lower() for w in ['offline', 'spill', 'power', 'stuck']): priority = 'High'
            
        ticket_id = 'REQ-' + str(random.randint(1000, 9999))
        
        c.execute('''INSERT INTO tickets 
            (ticket_id, user_name, role, department, building, location, issue, priority, ai_analysis, assigned_technician, status) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Unassigned', 'Pending')''', 
            (ticket_id, random_user, 'Campus User', dept, bldg, loc, issue, priority, 'User Submitted Request.'))
        count += 1
        
    conn.commit()
    conn.close()
    print(f"✅ Clean slate achieved! {count} human-user tickets successfully injected.")

if __name__ == "__main__":
    inject_historical_tickets()