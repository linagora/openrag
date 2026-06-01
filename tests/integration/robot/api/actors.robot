*** Settings ***
Resource    keywords.robot
Library     RequestsLibrary

*** Variables ***
${ACTORS_ENDPOINT}          ${BASE_URL}/actors
${RESTART_ACTOR_ENDPOINT}   ${ACTORS_ENDPOINT}/Vectordb/restart


*** Test Cases ***

List Ray Actors
    ${response}=    GET    ${ACTORS_ENDPOINT}    headers=${HEADERS}    expected_status=200
    ${json}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${json}    actors

Restart Vectordb Actor
    ${response}=    POST    ${RESTART_ACTOR_ENDPOINT}    headers=${HEADERS}    expected_status=200
    ${json}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${json}    message
    Should Contain    ${json}[message]    restarted successfully
    Dictionary Should Contain Key    ${json}    actor_id
