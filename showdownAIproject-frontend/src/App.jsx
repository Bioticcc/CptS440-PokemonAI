import './App.css'
import { useEffect, useState } from 'react'

// labeled dynamic data placeholders with TODO
// note to self: fix resizing using clamp. OR, make the pokedex fill the whole screen.
// that way it can be more of a "pop up pokedex" which can be a bit like a screen overlay
// another option is to have the buttons hidden when we minimize

function App() {
  //  DYNAMIC DATA USE STATES
  const [battle, setBattle] = useState(null)
  const [turn, setTurn] = useState(1)

  // live polling of the battle state so we can update the UI
  useEffect(() => {
    const interval = setInterval(async () => {
      try {
        // accessing our API endpoint and loading the data
        const res = await fetch("http://127.0.0.1:8000/state")
        const data = await res.json()
        setBattle(data)
      } catch (err) {
        console.error("Failed to fetch battle state:", err)
      }
    }, 1000) // polling every second

    return () => clearInterval(interval)
  }, [])

  // placeholder loading state, can make it more fun later with a pokeball or something
  // prevent rendering of the app before we have any data
  if (!battle) return <div>Loading battle...</div>

  return (
    // main app shell
    <div className="app-shell">

      {/* border to hold all coontent */}
      <div className="pokedex-border">
        <div className="pokedex-inner">

          {/* left panel */}
          <section className="panel panel-left">

            {/* circles of top panel */}
            <div className="circle-container">
              <div className="big-circle" />
              <div className="small-circle" style={{ backgroundColor: '#f92617' }} />
              <div className="small-circle" style={{ backgroundColor: '#fbd447' }} />
              <div className="small-circle" style={{ backgroundColor: '#75ff53' }} />
            </div>

            {/* curved line under circles */}
            <svg className="curve-line" viewBox="0 -20 300 80" preserveAspectRatio="none">
              <path d="M 0 46 L 132 46 Q 176 -8, 212 -8 L 300 -8"
                stroke="#c71e12" strokeWidth="8" fill="none" />
            </svg>

            {/* screen shell area */}
            <div className="screen-shell">

              {/* screen dots on thet op */}
              <div className="screen-dots">
                <div className="screen-dot" />
                <div className="screen-dot" />
              </div>

              {/* actual screen */}
              <div className="screen-display">

                {/* we'll have to add in stars, and the PP also */}
                {/* <div className="move-grid">
                  <div className="move-box">Move 1</div>
                  <div className="move-box">Move 2</div>
                  <div className="move-box">Move 3</div>
                  <div className="move-box">Move 4</div>
                  </div>
                </div> */}
                <div className="move-grid">
                  {battle?.legal_actions?.map((move, i) => (
                    <div className="move-box" key={i}>
                      <div><b>{move.move_name}</b></div>
                    </div>
                  ))}
                </div>

              </div>

              {/* onto the decorative buttons */}
              {/* row below screen */}
              <div className="left-buttons-row">
                {/* circle, 2 rectangles, main rectangle */}
                <div className="left-buttons-row-left">
                  <div className="left-button-circle" />
                  <div className="left-button-rectangle" style={{ backgroundColor: '#75ff53' }} />
                  <div className="left-button-rectangle" style={{ backgroundColor: '#ff783d' }} />
                </div>

                {/* TODO: large select button. actually might change this, since user can already click directly on moves*/}
                {/* maybe click that to switch to opponent view or something (display the known moves?) */}
                <div className="select-button">Select</div>

                {/* d-pad, just two rectangles */}
                <div className="left-buttons-row-right">
                  <div className="dpad">
                    <div className="dpad-horizontal" />
                    <div className="dpad-vertical" />
                  </div>
                </div>
              </div>

            </div>

          </section>

          {/* hinge panel - lines for looks */}
          <section className="panel panel-hinge">
            <div className="hinge-line" style={{ top: '5%' }} />
            <div className="hinge-line" style={{ top: '10%' }} />
            <div className="hinge-line" style={{ bottom: '10%' }} />
            <div className="hinge-line" style={{ bottom: '5%' }} />
          </section>

          {/* right panel */}
          <section className="panel panel-right">
            <div className="right-main-rectangle">

              {/* TODO: move reasons */}
              <p className="right-text">
                <span className="right-text-arrow">&#10148;</span>
                <strong><u>Reasons:</u></strong> new status bonus: +80.00; move order: +75.00
              </p>

              {/* TODO: opponent info (type, known moves, known switches) */}
              <p className="right-text">
                <span className="right-text-arrow">&#10148;</span>
                <strong><u>Opponent:</u></strong> {battle?.opponent?.active?.species}
              </p>
              <ul className="right-sublist">
                <li><strong>Type:</strong> {battle?.opponent?.active?.types?.join(', ')}</li>
                <li><strong>Known Moves:</strong> {battle?.opponent?.active?.known_moves?.join(', ')}</li>
              </ul>

            </div>

            {/* TODO: the pokemon switch options */}
            <div className="right-button-grid">

              {/* 1-5 is our other pokemon in our team */}
              <button className="right-blue-button">1</button>
              <button className="right-blue-button">2</button>
              <button className="right-blue-button">3</button>
              <button className="right-blue-button">4</button>
              <button className="right-blue-button">5</button>

              {/* 6-10 can hold the stars of how good the switch is, and status of pokemon (fainted? sleep?) */}
              <button className="right-blue-button">6</button>
              <button className="right-blue-button">7</button>
              <button className="right-blue-button">8</button>
              <button className="right-blue-button">9</button>
              <button className="right-blue-button">10</button>
            </div>

            {/* more decorative buttons, separate by row */}
            {/* row 1 has two small retangles on the right */}
            <div className="right-row-1">
              <div className="right-mini-rectangle" />
              <div className="right-mini-rectangle" />
            </div>

            {/* row 2 has one large gray button with a line between, and a circle */}
            <div className="right-row-2">
              <div className="right-gray-button">
                <div className="right-gray-line" />
              </div>
              <div className="right-circle" />
            </div>

            {/* row 3 has two big buttons */}
            <div className="right-row-3">
              <div className="right-large-rectangle" />
              <div className="right-large-rectangle" />
            </div>

          </section>

        </div>
      </div>
    </div>
  )
}

export default App
