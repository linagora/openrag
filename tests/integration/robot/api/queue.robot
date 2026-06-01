*** Settings ***
Resource    keywords.robot
Library     RequestsLibrary

*** Variables ***
${QUEUE_ENDPOINT}       ${BASE_URL}/queue
${QUEUE_INFO_ENDPOINT}  ${QUEUE_ENDPOINT}/info
${QUEUE_TASKS_ENDPOINT}  ${QUEUE_ENDPOINT}/tasks


*** Test Cases ***

Queue Info Endpoint
    ${response}=    GET    ${QUEUE_INFO_ENDPOINT}    headers=${HEADERS}    expected_status=200
    ${json}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${json}    workers
    Dictionary Should Contain Key    ${json}    tasks

Queue Tasks Endpoint
    ${response}=    GET    ${QUEUE_TASKS_ENDPOINT}    headers=${HEADERS}    expected_status=200
    ${json}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${json}    tasks
