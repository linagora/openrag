#!/usr/bin/env bash
set -euo pipefail

PARTITION_NAME=$1

# Thread 1
echo "Thread 1: root e-mail" \
    | .github/workflows/email_test/index_root_email.sh http://localhost:8080 ${PARTITION_NAME} thread1_root.txt thread1_root.txt

sleep 2s

echo "Thread 1 child 1 ( 1 . 1 ) Penguinone is an organic compound with the molecular formula C10H14O. Its name comes from the fact that its 2-dimensional molecular structure resembles a penguin." \
    | .github/workflows/email_test/index_child_email.sh http://localhost:8080 ${PARTITION_NAME} \
        thread1_child1.txt thread1_root.txt thread1_root.txt

sleep 2s

for k in `seq 1 3`;
do
    echo "Thread 1 leaf e-mail $k for child 1 ( 1 . 1 . $k )" \
        | .github/workflows/email_test/index_child_email.sh http://localhost:8080 ${PARTITION_NAME} \
            thread1_child1_leaf${k}.txt thread1_child1.txt thread1_root.txt
    echo
    echo thread1_child1_leaf${k}.txt
    sleep 2s
done

for j in `seq 2 3`;
do
    echo "Thread 1 child $j ( 1 . $j )" \
        | .github/workflows/email_test/index_child_email.sh http://localhost:8080 ${PARTITION_NAME} \
            thread1_child${j}.txt thread1_root.txt thread1_root.txt

    sleep 2s

    for k in `seq 1 3`;
    do
        echo "Thread 1 leaf e-mail $k for child $j ( 1 . $j . $k )" \
            | .github/workflows/email_test/index_child_email.sh http://localhost:8080 ${PARTITION_NAME} \
                thread1_child${j}_leaf${k}.txt thread1_child${j}.txt thread1_root.txt

        sleep 2s
    done
done


# Thread 2
echo "Thread 2: root e-mail" \
    | .github/workflows/email_test/index_root_email.sh http://localhost:8080 ${PARTITION_NAME} thread2_root.txt thread2_root.txt

sleep 2s

echo "Thread 1 child 1 ( 1 . 1 ) Administratively, Paris is divided into twenty arrondissements (districts), each having their own cultural identity." \
    | .github/workflows/email_test/index_child_email.sh http://localhost:8080 ${PARTITION_NAME} \
        thread2_child1.txt thread2_root.txt thread2_root.txt

sleep 2s

for k in `seq 1 3`;
do
    echo "Thread 2 leaf e-mail $k for child 1 ( 2 . 1 . $k )" \
        | .github/workflows/email_test/index_child_email.sh http://localhost:8080 ${PARTITION_NAME} \
            thread2_child1_leaf${k}.txt thread2_child1.txt thread2_root.txt

    sleep 2s
done

for j in `seq 2 3`;
do
    echo "Thread 2 child $j ( 2 . $j )" \
        | .github/workflows/email_test/index_child_email.sh http://localhost:8080 ${PARTITION_NAME} \
            thread2_child${j}.txt thread2_root.txt thread2_root.txt

    sleep 2s

    for k in `seq 1 3`;
    do
        echo "Thread 2 leaf e-mail $k for child $j ( 2 . $j . $k )" \
            | .github/workflows/email_test/index_child_email.sh http://localhost:8080 ${PARTITION_NAME} \
                thread2_child${j}_leaf${k}.txt thread2_child${j}.txt thread2_root.txt

        sleep 2s
    done
done

sleep 30s

# Verification : thread 1

# Positive tests
for target in thread1_root thread1_child1 thread1_child1_leaf1 thread1_child1_leaf2 thread1_child1_leaf3
do
    if echo "Penguinone" | .github/workflows/related_docs_test/chat_completion.sh http://localhost:8080 ${PARTITION_NAME} | grep file_id | grep $target
    then
        echo "Found $target"
    else
        echo "Not found $target"
        exit 1;
    fi
done

# Negative tests
for target in thread2 child2 child3
do
    if echo "Penguinone" | .github/workflows/related_docs_test/chat_completion.sh http://localhost:8080 ${PARTITION_NAME} | grep file_id | grep $target
    then
        echo "False positive: $target in the output"
        exit 1
    fi
done

# Verification : thread 2

# Positive tests
for target in thread2_root thread2_child1 thread2_child1_leaf1 thread2_child1_leaf2 thread2_child1_leaf3
do
    if echo "Paris" | .github/workflows/related_docs_test/chat_completion.sh http://localhost:8080 ${PARTITION_NAME} | grep file_id | grep $target
    then
        echo "Found $target"
    else
        echo "Not found $target"
        exit 1;
    fi
done

# Negative tests
for target in thread1 child2 child3
do
    if echo "Paris" | .github/workflows/related_docs_test/chat_completion.sh http://localhost:8080 ${PARTITION_NAME} | grep file_id | grep $target
    then
        echo "False positive: $target in the output"
        exit 1
    fi
done

