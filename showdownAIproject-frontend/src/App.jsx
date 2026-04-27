import './App.css'
import { useEffect, useState } from 'react'
import { fetchBattleState, fetchPrompt, submitPromptResponse } from './uiBridgeApi'

// labeled dynamic data placeholders with TODO

function App() {
  //  DYNAMIC DATA USE STATES
  const [battle, setBattle] = useState(null)
  const [prompt, setPrompt] = useState(null)
  const [promptError, setPromptError] = useState('')
  const [challengeName, setChallengeName] = useState('')
  const [isSubmittingPrompt, setIsSubmittingPrompt] = useState(false)

  // little tool tip to give the user some info
  const [showToolTip, setShowToolTip] = useState(false)

  // for switching pokemon, we need to know our current active pokemon and we hide it as an option
  const activeSpecies = battle?.friendly?.active?.species;

  // for reasons and move suggestions
  // here we'll just get the best moves in order and map the num of stars
  const rankedMoves = [...(battle?.legal_actions ?? [])]
    .sort((a, b) => b.score - a.score)

  const [selectedMove, setSelectedMove] = useState(null)
  const gridMoves = battle?.legal_actions ?? []

  // the reasons for moves
  const selectedMoveData = selectedMove

  // turn number
  const turnNumber = battle?.turn ?? 1

  // live polling of battle state + runtime prompts
  useEffect(() => {
    let mounted = true

    const tick = async () => {
      try {
        const [battleState, promptPayload] = await Promise.all([
          fetchBattleState(),
          fetchPrompt(),
        ])
        if (!mounted) {
          return
        }
        setBattle(battleState)
        const nextPrompt = promptPayload?.prompt ?? null
        setPrompt(nextPrompt)
        if (nextPrompt?.kind !== 'challenge_username') {
          setChallengeName('')
        }
      } catch (err) {
        if (!mounted) {
          return
        }
        console.error("Failed to fetch runtime UI state:", err)
      }
    }

    tick()
    const interval = setInterval(tick, 1000) // polling every second

    return () => {
      mounted = false
      clearInterval(interval)
    }
  }, [])

  async function submitChoice(choiceId) {
    if (!prompt) {
      return
    }

    setIsSubmittingPrompt(true)
    setPromptError('')
    try {
      await submitPromptResponse({
        prompt_id: prompt.prompt_id,
        choice_id: String(choiceId),
      })
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      setPromptError(message)
    } finally {
      setIsSubmittingPrompt(false)
    }
  }

  async function submitChallengeName(event) {
    event.preventDefault()
    if (!prompt) {
      return
    }

    const value = challengeName.trim()
    if (!value) {
      setPromptError('Enter a username before submitting.')
      return
    }

    setIsSubmittingPrompt(true)
    setPromptError('')
    try {
      await submitPromptResponse({
        prompt_id: prompt.prompt_id,
        value,
      })
      setChallengeName('')
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      setPromptError(message)
    } finally {
      setIsSubmittingPrompt(false)
    }
  }

  // placeholder loading state, can make it more fun later with a pokeball or something
  // prevent rendering of the app before we have any data
  if (!battle) return <div>Loading battle...</div>

  return (
    // main app shell
    <div className="app-shell">
      {prompt && (
        <section className="runtime-menu-card">
          <div className="runtime-menu-title">Runtime Menu</div>
          <div className="runtime-menu-message">{prompt.message}</div>

          {prompt.kind === 'main_menu' && (
            <div className="runtime-menu-options">
              <button className="runtime-menu-button" onClick={() => submitChoice('1')} disabled={isSubmittingPrompt}>
                1. Connect to ladder
              </button>
              <button className="runtime-menu-button" onClick={() => submitChoice('2')} disabled={isSubmittingPrompt}>
                2. Challenge a player
              </button>
              <button className="runtime-menu-button" onClick={() => submitChoice('3')} disabled={isSubmittingPrompt}>
                3. View all-time winrate
              </button>
              <button className="runtime-menu-button" onClick={() => submitChoice('4')} disabled={isSubmittingPrompt}>
                4. Quit
              </button>
            </div>
          )}

          {prompt.kind === 'challenge_username' && (
            <form className="runtime-menu-form" onSubmit={submitChallengeName}>
              <input
                className="runtime-menu-input"
                placeholder="Showdown username"
                value={challengeName}
                onChange={(event) => setChallengeName(event.target.value)}
                disabled={isSubmittingPrompt}
              />
              <button className="runtime-menu-button" type="submit" disabled={isSubmittingPrompt}>
                Send Challenge
              </button>
            </form>
          )}

          {prompt.kind !== 'main_menu' && prompt.kind !== 'challenge_username' && (
            <div className="runtime-menu-options">
              {(prompt.options || []).map((option) => (
                <button
                  key={option.id}
                  className="runtime-menu-button"
                  onClick={() => submitChoice(option.id)}
                  disabled={isSubmittingPrompt}
                >
                  {option.label || option.id}
                </button>
              ))}
            </div>
          )}

          {promptError && <div className="runtime-menu-error">{promptError}</div>}
        </section>
      )}

      {/* border to hold all coontent */}
      <div className="pokedex-border">
        <div className="pokedex-inner">

          {/* tool tip */}
          <div
            className="tooltip-trigger"
            onMouseEnter={() => setShowToolTip(true)}
            onMouseLeave={() => setShowToolTip(false)}
          >
            ?
          </div>

          {showToolTip && (
            <div className="tooltip-popup">
              Moves are recommended using a star system, with 3 stars being the highest. <br /><br />
              Click on a move to see our model's reasoning.
            </div>
          )}

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
                <div className="move-grid">

                  {/* mapping our legal actions - a user will select a move and reasons show on the right later */}
                  {gridMoves.map((move, i) => (
                    <div
                      key={move.action_id}
                      className="move-box"
                      onClick={() => {
                        setSelectedMove(move)
                      }}
                      style={{
                        cursor: 'pointer',
                        border:

                          selectedMove?.action_id === move.action_id
                            ? "3px solid #17f944"
                            : "3px solid #333333"
                      }}
                    >

                      {/* name of the move*/}
                      <div><b>{move.move_name}</b></div>

                      {/* PP */}
                      <div>PP: {move.current_pp ?? "?"}/{move.max_pp ?? "?"}</div>

                      {/* stars to indicate the model rating */}
                      <div>
                        {move.rank === 1 && "★★★"}
                        {move.rank === 2 && "★★"}
                        {move.rank === 3 && "★"}
                      </div>

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

                {/* TODO: hook this up with a select move */}
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

              {/* move reasons */}
              <p className="right-text">
                <span className="right-text-arrow">&#10148;</span>
                <strong><u>
                  {selectedMoveData ? selectedMoveData.move_name : ""} Reasons:
                </u></strong>
              </p>

              {selectedMoveData ? (
                <ul className="right-sublist">
                  {/* list of reasons */}
                  {selectedMoveData.reasons?.length > 0 ? (
                    selectedMoveData.reasons.map((reason, i) => (
                      <li key={i}>{reason}</li>
                    ))
                  ) : (
                    <li>No reasoning available.</li>
                  )}
                </ul>
              ) : (
                // when no move is selected (default)
                <p className="right-text">Click a move to see its reasoning.</p>
              )}

              {/* opponent info (type, known moves, known switches) */}
              <p className="right-text">
                <span className="right-text-arrow">&#10148;</span>
                <strong><u>Opponent:</u></strong> {battle?.opponent?.active?.species}
              </p>
              <ul className="right-sublist">
                <li><strong>Type:</strong> {battle?.opponent?.active?.types?.join(', ')}</li>
                <li><strong>Known Moves:</strong> {battle?.opponent?.active?.known_moves?.join(', ')}</li>
                <li><strong>Known Switches:</strong> {battle?.opponent?.active?.known_switches?.join(', ')}</li>
              </ul>

            </div>

            {/* TODO: the pokemon switch options */}
            <div className="right-button-grid">

              {/* 1-5 is our other pokemon in our team */}
              {/* map our pokemon, we'll ignore our current pokemon */}
              {battle?.friendly?.team
                ?.filter(pokemon => pokemon.species !== activeSpecies)
                .slice(0, 5)
                .map((pokemon, i) => (
                  <button className="right-blue-button" key={i}>
                    {pokemon.species}
                  </button>
                ))}

              {/* 6-10 can hold the stars of how good the switch is, and status of pokemon (fainted? sleep?) */}
              {battle?.friendly?.team
                ?.filter(pokemon => pokemon.species !== activeSpecies)
                .map((pokemon, i) => (
                  <button className="right-blue-button" key={i}>

                    {/* status and info */}
                    <span className="switch-status-text">
                      {pokemon.fainted ? "fainted" : ""}
                      {pokemon.status ? `${pokemon.status}` : ""}
                      {!pokemon.fainted && !pokemon.status ? "healthy" : ""}

                      {/* TODO: indicate star rating */}
                      {/* <div>★</div> */}
                    </span>
                  </button>
                ))}

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
              <div className="right-large-rectangle">
                <strong> Turn {battle?.turn ?? "?"}</strong>
              </div>
              <div className="right-large-rectangle" />
            </div>

          </section>

        </div>
      </div>
    </div>
  )
}

export default App
