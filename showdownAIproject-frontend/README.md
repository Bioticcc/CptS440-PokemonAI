# RUNNING FRONTEND
To run the frontend, run the following commands:

```bash
cd showdownAIproject-frontend
npm install
npm run dev
```

Then, open up the local link, given in the terminal, in your chosen browser (ex: http://localhost:5173).

# NOTES
The frontend is currently a WIP! Currently, need to complete:
- Resizing options:
    - Do we want to make the Pokedex take up the full screen? 
        - It will look more of a floating box/screen (similar to a minimized YouTube video that you can have on a device)

    - Or, do we want the Pokedex to minimize so it is side-by-side to the browser?
        - We can then have an option to hide decorative buttons if we minimize the screen enough.

- Lastly, will need to connect this all to the actual backend. Shouldn't take as long as designing frontend
    - Dynamic data is labeled with TODO in the App.jsx. Will just take some simple routing to fill in the gaps

    - Using FastAPI: 
        - Install using pip install fastapi uvicorn
        - Run in terminal, side by side: 
        PYTHONPATH=src python -m psai.app.mock_stream (this is sample info)
        PYTHONPATH=src uvicorn psai.app.ui_server:app --reload (running the actual API server)

# RELEVANT BACKEND FILES
- psai/app/mock_stream.py
    - Offline testing for UI and backend connection, fake battles for now without connecting to the actual acc
    - I was having issues with setting up bot accounts so I decided to go with this

- psai/app/ui_payload.py
    - Payloads from backend end being sent over for frontend rendering

- psai/app/ui_server.py
    - FastAPI backend server for exposing the UI data to the frontend, with endpoints (state, steam)great