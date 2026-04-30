import './App.css'
import { useEffect, useMemo, useRef, useState } from 'react'
import { fetchBattleState, fetchPrompt, fetchUiLogs, submitPromptResponse } from './uiBridgeApi'

const TURN_PROMPT_KINDS = new Set([
  'battle_mode',
  'attack_slot',
  'switch_slot',
  'forfeit_confirm',
])

const MENU_PROMPT_KINDS = new Set(['main_menu', 'challenge_username'])

function normalizeToken(value) {
  return String(value ?? '')
    .toLowerCase()
    .replace(/[^a-z0-9]/g, '')
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds))
}

function isStalePromptError(error) {
  const message = error instanceof Error ? error.message : String(error)
  return message.includes('stale_prompt')
}

function stripRecommendationSuffix(label) {
  return String(label ?? '')
    .split(' (recommended')[0]
    .trim()
}

function resolvePromptSwitchSlot(promptSnapshot, preferredSpecies, fallbackSlot) {
  const options = Array.isArray(promptSnapshot?.options) ? promptSnapshot.options : []
  if (options.length <= 0) {
    return null
  }

  const preferredKey = normalizeToken(preferredSpecies)
  if (preferredKey) {
    for (const option of options) {
      const optionLabel = stripRecommendationSuffix(option?.label ?? option?.id)
      if (normalizeToken(optionLabel) === preferredKey) {
        return String(option?.id)
      }
    }
  }

  if (fallbackSlot > 0) {
    const fallbackId = String(fallbackSlot)
    if (options.some((option) => String(option?.id) === fallbackId)) {
      return fallbackId
    }
  }

  return String(options[0]?.id)
}

function switchSpeciesNameFromAction(action) {
  const raw = String(action?.move_name ?? '')
  if (raw.toLowerCase().startsWith('switch_')) {
    return raw.slice('switch_'.length)
  }
  return raw || 'switch'
}

function App() {
  const [battle, setBattle] = useState(null)
  const [prompt, setPrompt] = useState(null)
  const [promptError, setPromptError] = useState('')
  const [challengeName, setChallengeName] = useState('')
  const [isSubmittingPrompt, setIsSubmittingPrompt] = useState(false)
  const [showToolTip, setShowToolTip] = useState(false)
  const [isQueueing, setIsQueueing] = useState(false)
  const [queueMessage, setQueueMessage] = useState('Currently queueing for a game')
  const [menuStatusLines, setMenuStatusLines] = useState([])
  const logCursorRef = useRef(0)

  const [selectedMoveActionId, setSelectedMoveActionId] = useState('')
  const [selectedSwitchActionId, setSelectedSwitchActionId] = useState('')
  const [submittedTurnKey, setSubmittedTurnKey] = useState('')
  const [lastSeenTurnKey, setLastSeenTurnKey] = useState('')
  const promptKind = prompt?.kind ?? ''

  const legalActions = battle?.legal_actions ?? []
  const moveActions = legalActions.filter((action) => !action.is_switch)
  const switchActions = legalActions.filter((action) => Boolean(action.is_switch))
  const primaryActionCards = moveActions.length > 0 ? moveActions : switchActions

  const activeSpecies = battle?.friendly?.active?.species ?? ''
  const benchTeam = (battle?.friendly?.team ?? []).filter(
    (pokemon) => pokemon.species !== activeSpecies
  )

  const selectedPrimaryAction =
    primaryActionCards.find((action) => action.action_id === selectedMoveActionId) ?? primaryActionCards[0] ?? null

  const switchCandidates = useMemo(() => {
    const seenSwitches = new Set()
    const candidates = []

    for (const pokemon of benchTeam) {
      const speciesKey = normalizeToken(pokemon.species)
      const matchedSwitch = switchActions.find((action) => {
        if (seenSwitches.has(action.action_id)) {
          return false
        }

        const actionSpecies = String(action.move_name ?? '').replace(/^switch_/i, '')
        const actionSpeciesKey = normalizeToken(actionSpecies)
        const actionIdKey = normalizeToken(action.action_id)

        return actionSpeciesKey === speciesKey || actionIdKey.includes(speciesKey)
      })

      if (!matchedSwitch) {
        continue
      }

      seenSwitches.add(matchedSwitch.action_id)
      candidates.push({
        pokemon,
        actionId: matchedSwitch.action_id,
      })
    }

    return candidates
  }, [benchTeam, switchActions])

  const promptSwitchCandidates = useMemo(() => {
    if (promptKind !== 'switch_slot') {
      return []
    }

    const options = Array.isArray(prompt?.options) ? prompt.options : []
    return options.map((option) => {
      const slotId = String(option?.id ?? '')
      const optionSpecies = stripRecommendationSuffix(option?.label ?? slotId)
      const matchedTeamPokemon = benchTeam.find(
        (pokemon) => normalizeToken(pokemon.species) === normalizeToken(optionSpecies)
      )

      return {
        pokemon: matchedTeamPokemon || {
          species: optionSpecies || `switch_${slotId}`,
          fainted: false,
          status: null,
        },
        actionId: `slot:${slotId}`,
      }
    })
  }, [benchTeam, prompt, promptKind])

  const displayedSwitchCandidates =
    promptSwitchCandidates.length > 0 ? promptSwitchCandidates : switchCandidates

  const currentTurnKey =
    battle?.battle_tag && battle?.battle_tag !== 'no_battle'
      ? `${battle.battle_tag}::${battle.turn ?? 0}`
      : ''

  const isMenuPrompt = MENU_PROMPT_KINDS.has(promptKind)
  const isTurnPrompt = TURN_PROMPT_KINDS.has(promptKind)
  const hasBattleState = Boolean(battle?.battle_tag && battle.battle_tag !== 'no_battle')

  const isWaitingOnOpponent =
    Boolean(currentTurnKey) &&
    submittedTurnKey === currentTurnKey &&
    !isTurnPrompt

  const turnSelectionStatus = isWaitingOnOpponent
    ? 'Waiting on opponent'
    : (moveActions.length > 0 ? 'Please select a move' : 'Please select a switch')

  const canSubmitMove = promptKind === 'battle_mode' || promptKind === 'attack_slot'
  const canSubmitSwitch = promptKind === 'battle_mode' || promptKind === 'switch_slot'
  const canSubmitForfeit = promptKind === 'battle_mode' || promptKind === 'forfeit_confirm'

  useEffect(() => {
    if (!selectedPrimaryAction) {
      setSelectedMoveActionId('')
      return
    }
    if (selectedMoveActionId !== selectedPrimaryAction.action_id) {
      setSelectedMoveActionId(selectedPrimaryAction.action_id)
    }
  }, [selectedMoveActionId, selectedPrimaryAction])

  useEffect(() => {
    if (displayedSwitchCandidates.length === 0) {
      setSelectedSwitchActionId('')
      return
    }

    const hasSelectedSwitch = displayedSwitchCandidates.some(
      (candidate) => candidate.actionId === selectedSwitchActionId
    )
    if (!hasSelectedSwitch) {
      setSelectedSwitchActionId(displayedSwitchCandidates[0].actionId)
    }
  }, [displayedSwitchCandidates, selectedSwitchActionId])

  useEffect(() => {
    if (!currentTurnKey) {
      if (lastSeenTurnKey) {
        setLastSeenTurnKey('')
      }
      if (submittedTurnKey) {
        setSubmittedTurnKey('')
      }
      return
    }

    if (currentTurnKey !== lastSeenTurnKey) {
      setLastSeenTurnKey(currentTurnKey)
      setSubmittedTurnKey('')
    }
  }, [currentTurnKey, lastSeenTurnKey, submittedTurnKey])

  useEffect(() => {
    let mounted = true

    const tick = async () => {
      try {
        const [battleState, promptPayload, logsPayload] = await Promise.all([
          fetchBattleState(),
          fetchPrompt(),
          fetchUiLogs(logCursorRef.current),
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

        if (
          nextPrompt?.kind === 'main_menu' ||
          nextPrompt?.kind === 'challenge_username' ||
          TURN_PROMPT_KINDS.has(nextPrompt?.kind)
        ) {
          setIsQueueing(false)
        }

        if (logsPayload && typeof logsPayload === 'object') {
          const nextCursor = Number(logsPayload.cursor)
          if (Number.isFinite(nextCursor) && nextCursor >= 0) {
            logCursorRef.current = nextCursor
          }

          const items = Array.isArray(logsPayload.items) ? logsPayload.items : []
          const nextLines = []
          for (const item of items) {
            const line = String(item?.line ?? '').trim()
            if (!line) {
              continue
            }
            if (line === '==============================================') {
              continue
            }
            if (line === 'Pokemon AI Console Menu') {
              continue
            }
            if (/^[1-4]\.\s/.test(line)) {
              continue
            }
            nextLines.push(line)
          }

          if (nextLines.length > 0) {
            setMenuStatusLines((previous) => [...previous, ...nextLines].slice(-8))
          }
        }
      } catch (error) {
        if (!mounted) {
          return
        }
        console.error('Failed to fetch runtime UI state:', error)
      }
    }

    tick()
    const interval = setInterval(tick, 1000)

    return () => {
      mounted = false
      clearInterval(interval)
    }
  }, [])

  async function waitForPromptKind(expectedKinds, timeoutMilliseconds = 6000) {
    const allowedKinds = new Set(expectedKinds)
    const deadline = Date.now() + Math.max(500, timeoutMilliseconds)

    while (Date.now() < deadline) {
      const promptPayload = await fetchPrompt()
      const nextPrompt = promptPayload?.prompt ?? null
      setPrompt(nextPrompt)

      if (nextPrompt?.kind && allowedKinds.has(nextPrompt.kind)) {
        return nextPrompt
      }

      await sleep(120)
    }

    return null
  }

  async function submitWithStaleRetry(buildPayload) {
    if (!prompt) {
      throw new Error('no_active_prompt')
    }

    let activePrompt = prompt
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        await submitPromptResponse(buildPayload(activePrompt))
        return activePrompt
      } catch (error) {
        if (attempt === 0 && isStalePromptError(error)) {
          const refreshed = await fetchPrompt()
          activePrompt = refreshed?.prompt ?? null
          setPrompt(activePrompt)
          continue
        }
        throw error
      }
    }

    throw new Error('prompt_submit_failed:stale_prompt')
  }

  async function submitChoice(choiceId) {
    if (!prompt) {
      return
    }

    setIsSubmittingPrompt(true)
    setPromptError('')
    try {
      const submittedPrompt = await submitWithStaleRetry((activePrompt) => ({
        prompt_id: activePrompt.prompt_id,
        choice_id: String(choiceId),
      }))

      if (submittedPrompt?.kind === 'main_menu' && String(choiceId) === '1') {
        setIsQueueing(true)
        setQueueMessage('Currently queueing for a game')
        setPrompt(null)
      }
      if (submittedPrompt?.kind === 'challenge_username' && String(choiceId).toLowerCase() === 'back') {
        setChallengeName('')
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
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
      await submitWithStaleRetry((activePrompt) => ({
        prompt_id: activePrompt.prompt_id,
        value,
      }))
      setChallengeName('')
      setIsQueueing(true)
      setQueueMessage(`Waiting for ${value} to accept challenge`)
      setPrompt(null)
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      setPromptError(message)
    } finally {
      setIsSubmittingPrompt(false)
    }
  }

  async function submitSelectedMove() {
    if (!prompt) {
      setPromptError('Action is unavailable while waiting on the opponent.')
      return
    }
    if (!selectedPrimaryAction || selectedPrimaryAction.is_switch) {
      setPromptError('Select a move first.')
      return
    }

    const moveSlot = moveActions.findIndex(
      (action) => action.action_id === selectedPrimaryAction.action_id
    ) + 1
    if (moveSlot <= 0) {
      setPromptError('Selected move is no longer available this turn.')
      return
    }

    setIsSubmittingPrompt(true)
    setPromptError('')
    try {
      let activePrompt = prompt

      if (activePrompt.kind === 'battle_mode') {
        await submitPromptResponse({
          prompt_id: activePrompt.prompt_id,
          choice_id: '1',
        })
        activePrompt = await waitForPromptKind(['attack_slot'])
      }

      if (!activePrompt || activePrompt.kind !== 'attack_slot') {
        throw new Error('attack_prompt_unavailable')
      }

      await submitPromptResponse({
        prompt_id: activePrompt.prompt_id,
        choice_id: String(moveSlot),
      })
      setSubmittedTurnKey(currentTurnKey)
      setPrompt(null)
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      setPromptError(message)
    } finally {
      setIsSubmittingPrompt(false)
    }
  }

  async function submitSelectedSwitch() {
    if (!prompt) {
      setPromptError('Action is unavailable while waiting on the opponent.')
      return
    }

    const selectedSwitch = displayedSwitchCandidates.find(
      (candidate) => candidate.actionId === selectedSwitchActionId
    )
    if (!selectedSwitch) {
      setPromptError('Select a switch target first.')
      return
    }

    const selectedSpecies = String(selectedSwitch?.pokemon?.species ?? '').trim()
    const promptSlotMatch = /^slot:(.+)$/.exec(String(selectedSwitch.actionId))
    const slotFromPromptList = promptSlotMatch ? Number(promptSlotMatch[1]) : NaN
    const slotFromStateActions = switchActions.findIndex(
      (action) => action.action_id === selectedSwitch.actionId
    ) + 1
    const fallbackSwitchSlot = Number.isFinite(slotFromPromptList) && slotFromPromptList > 0
      ? slotFromPromptList
      : slotFromStateActions

    setIsSubmittingPrompt(true)
    setPromptError('')
    try {
      let activePrompt = prompt

      if (activePrompt.kind === 'battle_mode') {
        await submitPromptResponse({
          prompt_id: activePrompt.prompt_id,
          choice_id: '2',
        })
        activePrompt = await waitForPromptKind(['switch_slot'])
      }

      if (!activePrompt || activePrompt.kind !== 'switch_slot') {
        throw new Error('switch_prompt_unavailable')
      }

      const resolvedSlotChoice = resolvePromptSwitchSlot(
        activePrompt,
        selectedSpecies,
        fallbackSwitchSlot,
      )
      if (!resolvedSlotChoice) {
        throw new Error('switch_slot_unresolved')
      }

      await submitPromptResponse({
        prompt_id: activePrompt.prompt_id,
        choice_id: String(resolvedSlotChoice),
      })
      setSubmittedTurnKey(currentTurnKey)
      setPrompt(null)
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      setPromptError(message)
    } finally {
      setIsSubmittingPrompt(false)
    }
  }

  async function submitForfeit() {
    if (!prompt) {
      setPromptError('Forfeit is unavailable while waiting on the opponent.')
      return
    }

    setIsSubmittingPrompt(true)
    setPromptError('')
    try {
      let activePrompt = prompt

      if (activePrompt.kind === 'battle_mode') {
        await submitPromptResponse({
          prompt_id: activePrompt.prompt_id,
          choice_id: '3',
        })
        activePrompt = await waitForPromptKind(['forfeit_confirm'])
      }

      if (!activePrompt || activePrompt.kind !== 'forfeit_confirm') {
        throw new Error('forfeit_prompt_unavailable')
      }

      await submitPromptResponse({
        prompt_id: activePrompt.prompt_id,
        choice_id: 'confirm',
      })
      setPrompt(null)
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      setPromptError(message)
    } finally {
      setIsSubmittingPrompt(false)
    }
  }

  if (!battle) {
    return <div className="app-loading">Loading battle...</div>
  }

  if (isMenuPrompt) {
    return (
      <div className="app-shell menu-shell main-menu-shell">
        <section className="main-menu-card">
          <div className="main-menu-banner">
            <h1 className="main-menu-title">PokeLearn</h1>
            <p className="main-menu-subtitle">A trained model exceptionally skilled in choosing Body Slam at inopportune times!</p>
          </div>

          {prompt?.kind === 'main_menu' && (
            <div className="main-menu-options">
              <button
                className="main-menu-button"
                onClick={() => submitChoice('1')}
                disabled={isSubmittingPrompt}
              >
                Connect to ladder
              </button>
              <button
                className="main-menu-button"
                onClick={() => submitChoice('2')}
                disabled={isSubmittingPrompt}
              >
                Challenge a player
              </button>
              <button
                className="main-menu-button"
                onClick={() => submitChoice('3')}
                disabled={isSubmittingPrompt}
              >
                View all-time winrate
              </button>
              <button
                className="main-menu-button"
                onClick={() => submitChoice('4')}
                disabled={isSubmittingPrompt}
              >
                Quit
              </button>
            </div>
          )}

          {prompt?.kind === 'challenge_username' && (
            <form className="main-menu-form" onSubmit={submitChallengeName}>
              <input
                className="main-menu-input"
                placeholder="Showdown username"
                value={challengeName}
                onChange={(event) => setChallengeName(event.target.value)}
                disabled={isSubmittingPrompt}
              />
              <button
                className="main-menu-button"
                type="submit"
                disabled={isSubmittingPrompt}
              >
                Send challenge
              </button>
              <button
                className="main-menu-button back-button"
                type="button"
                onClick={() => submitChoice('back')}
                disabled={isSubmittingPrompt}
              >
                Back to main menu
              </button>
            </form>
          )}

          {menuStatusLines.length > 0 && (
            <div className="menu-status-log">
              {menuStatusLines.map((line, index) => (
                <div className="menu-status-line" key={`${index}-${line}`}>
                  {line}
                </div>
              ))}
            </div>
          )}

          {promptError && <div className="runtime-menu-error">{promptError}</div>}
        </section>
      </div>
    )
  }

  if (isQueueing) {
    return (
      <div className="app-shell menu-shell main-menu-shell">
        <section className="queue-card">
          <div className="main-menu-banner">
            <h1 className="main-menu-title">PokeLearn</h1>
            <p className="main-menu-subtitle">A trained model exceptionally skilled in choosing Body Slam at inopportune times!</p>
          </div>

          <p className="queue-text">{queueMessage}</p>
          <div className="queue-indicator" aria-label="searching">
            <span className="queue-dot" />
            <span className="queue-dot" />
            <span className="queue-dot" />
          </div>
          {menuStatusLines.length > 0 && (
            <div className="menu-status-log">
              {menuStatusLines.map((line, index) => (
                <div className="menu-status-line" key={`${index}-${line}`}>
                  {line}
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
    )
  }

  if (!hasBattleState && !isTurnPrompt) {
    return <div className="app-loading">Waiting for battle state...</div>
  }

  return (
    <div className="app-shell">
      <div className="pokedex-border">
        <div className="pokedex-inner">
          <div
            className="tooltip-trigger"
            onMouseEnter={() => setShowToolTip(true)}
            onMouseLeave={() => setShowToolTip(false)}
          >
            ?
          </div>

          {showToolTip && (
            <div className="tooltip-popup">
              Moves are recommended using a star system, with 3 stars being the highest.
              <br />
              <br />
              Click a move to see our model&apos;s reasoning.
            </div>
          )}

          <section className="panel panel-left">
            <div className="circle-container">
              <div className="big-circle" />
              <div className="small-circle" style={{ backgroundColor: '#f92617' }} />
              <div className="small-circle" style={{ backgroundColor: '#fbd447' }} />
              <div className="small-circle" style={{ backgroundColor: '#75ff53' }} />
            </div>

            <svg className="curve-line" viewBox="0 -20 300 80" preserveAspectRatio="none">
              <path
                d="M 0 46 L 132 46 Q 176 -8, 212 -8 L 300 -8"
                stroke="#c71e12"
                strokeWidth="8"
                fill="none"
              />
            </svg>

            <div className="screen-shell">
              <div className="screen-dots">
                <div className="screen-dot" />
                <div className="screen-dot" />
              </div>

              <div className="screen-display">
                <div className="turn-status-banner">{turnSelectionStatus}</div>
                <div className="move-grid">
                  {primaryActionCards.map((move) => (
                    <div
                      key={move.action_id}
                      className="move-box"
                      onClick={() => {
                        setSelectedMoveActionId(move.action_id)
                        if (!move.is_switch) {
                          return
                        }
                        const preferredSpecies = switchSpeciesNameFromAction(move)
                        const bySpecies = displayedSwitchCandidates.find(
                          (candidate) => normalizeToken(candidate?.pokemon?.species) === normalizeToken(preferredSpecies)
                        )
                        if (bySpecies) {
                          setSelectedSwitchActionId(bySpecies.actionId)
                          return
                        }
                        const byActionId = displayedSwitchCandidates.find(
                          (candidate) => candidate.actionId === move.action_id
                        )
                        if (byActionId) {
                          setSelectedSwitchActionId(byActionId.actionId)
                        }
                      }}
                      style={{
                        cursor: 'pointer',
                        border:
                          selectedPrimaryAction?.action_id === move.action_id
                            ? '3px solid #17f944'
                            : '3px solid #333333',
                      }}
                    >
                      <div><b>{move.is_switch ? switchSpeciesNameFromAction(move) : move.move_name}</b></div>
                      <div>
                        {move.is_switch
                          ? 'Switch option'
                          : `PP: ${move.current_pp ?? '?'}/${move.max_pp ?? '?'}`}
                      </div>
                      <div>
                        {move.rank === 1 && '★★★'}
                        {move.rank === 2 && '★★'}
                        {move.rank === 3 && '★'}
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              <div className="left-buttons-row">
                <div className="left-buttons-row-left">
                  <div className="left-button-circle" />
                  <div
                    className="left-button-rectangle"
                    style={{ backgroundColor: '#75ff53' }}
                  />
                  <div
                    className="left-button-rectangle"
                    style={{ backgroundColor: '#ff783d' }}
                  />
                </div>

                <button
                  className="select-button"
                  onClick={submitSelectedMove}
                  disabled={
                    !canSubmitMove ||
                    !selectedPrimaryAction ||
                    selectedPrimaryAction.is_switch ||
                    moveActions.length <= 0 ||
                    isSubmittingPrompt
                  }
                >
                  Select
                </button>

                <div className="left-buttons-row-right">
                  <div className="dpad">
                    <div className="dpad-horizontal" />
                    <div className="dpad-vertical" />
                  </div>
                </div>
              </div>
            </div>
          </section>

          <section className="panel panel-hinge">
            <div className="hinge-line" style={{ top: '5%' }} />
            <div className="hinge-line" style={{ top: '10%' }} />
            <div className="hinge-line" style={{ bottom: '10%' }} />
            <div className="hinge-line" style={{ bottom: '5%' }} />
          </section>

          <section className="panel panel-right">
            <div className="right-main-rectangle">
              <p className="right-text">
                <span className="right-text-arrow">&#10148;</span>
                <strong><u>{
                  selectedPrimaryAction
                    ? (
                        selectedPrimaryAction.is_switch
                          ? `Switch: ${switchSpeciesNameFromAction(selectedPrimaryAction)}`
                          : selectedPrimaryAction.move_name
                      )
                    : ''
                } Reasons:</u></strong>
              </p>

              {selectedPrimaryAction ? (
                <ul className="right-sublist">
                  <li><strong>Total score:</strong> {Number(selectedPrimaryAction.score ?? 0).toFixed(2)}</li>
                  {selectedPrimaryAction.reasons?.length > 0 ? (
                    selectedPrimaryAction.reasons.map((reason, index) => (
                      <li key={index}>{reason}</li>
                    ))
                  ) : (
                    <li>No reasoning available.</li>
                  )}
                </ul>
              ) : (
                <p className="right-text">Click a move to see its reasoning.</p>
              )}

              <p className="right-text">
                <span className="right-text-arrow">&#10148;</span>
                <strong><u>Opponent:</u></strong> {battle?.opponent?.active?.species}
              </p>
              <ul className="right-sublist">
                <li><strong>Type:</strong> {battle?.opponent?.active?.types?.join(', ')}</li>
                <li><strong>Known Moves:</strong> {battle?.opponent?.active?.known_moves?.join(', ')}</li>
              </ul>
            </div>

            <div className="right-button-grid">
              {displayedSwitchCandidates.map(({ pokemon, actionId }) => (
                <button
                  className={`right-blue-button ${selectedSwitchActionId === actionId ? 'selected' : ''}`}
                  key={actionId}
                  onClick={() => setSelectedSwitchActionId(actionId)}
                >
                  <span className="switch-species-text">{pokemon.species}</span>
                  <span className="switch-status-text">
                    {pokemon.fainted
                      ? 'fainted'
                      : pokemon.status
                        ? String(pokemon.status)
                        : 'healthy'}
                  </span>
                </button>
              ))}
            </div>

            <div className="right-row-1">
              <div className="right-mini-rectangle" />
              <div className="right-mini-rectangle" />
            </div>

            <div className="right-row-2">
              <button
                className="select-button switch-button"
                onClick={submitSelectedSwitch}
                disabled={!canSubmitSwitch || !selectedSwitchActionId || isSubmittingPrompt}
              >
                Switch
              </button>
            </div>

            <div className="right-row-3">
              <div className="right-large-rectangle">
                <strong>Turn {battle?.turn ?? '?'}</strong>
              </div>
              <button
                className="right-large-rectangle right-forfeit-button"
                onClick={submitForfeit}
                disabled={!canSubmitForfeit || isSubmittingPrompt}
              >
                Forfeit
              </button>
            </div>

            {promptError && <div className="runtime-menu-error battle-error">{promptError}</div>}
          </section>
        </div>
      </div>
    </div>
  )
}

export default App
