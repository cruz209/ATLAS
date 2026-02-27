from services.db import init_db, seed_demo_world

if __name__ == "__main__":
    init_db()
    seed_demo_world()
    print("Initialized demo_world.db with seed entities.")
