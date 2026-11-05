# Name:        run_to_file.cmake
# Description: runs a Python script and writes what it prints to a file (MAME's m6809make.py prints the steps it
#              writes from a .lst), in a way every CMake generator runs the same
# Version:     0.1.0
# Created:     05-11-2026
# Modified:    05-11-2026
# Author:      Sebastian Toyos
#
# cmake -DPYTHON=<python> -DSCRIPT=<script.py> -DINPUT=<argument> -DOUTPUT=<file> -P run_to_file.cmake
execute_process(COMMAND ${PYTHON} ${SCRIPT} ${INPUT} OUTPUT_FILE ${OUTPUT} RESULT_VARIABLE K_RESULT)
if(NOT K_RESULT EQUAL 0)
	file(REMOVE ${OUTPUT})
	message(FATAL_ERROR "${SCRIPT} ${INPUT} failed (${K_RESULT})")
endif()
