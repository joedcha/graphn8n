#!/bin/bash
#
#Script Name    : run_executions.sh
#
#******************************************************************************
#* Version   Fecha Inicio              Author                 Description     *
#* -------  --------------   --------------------------   ------------------  *
#*  1.0      04/08/2026              n8n-zabbix-bridge     Duracion/estado/timing por ejecucion n8n -> Zabbix *
#******************************************************************************
export PATHLOG1=/opt/bmc/ETLs/n8n/monworkflows
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8
cd $PATHLOG1 || exit 1

echo "Inicio ETL n8n executions" >> $PATHLOG1/logsh.txt
date >> $PATHLOG1/logsh.txt

flock -w 30 $PATHLOG1/.state.lock \
  $PATHLOG1/python/bin/python3 $PATHLOG1/monitor.py --mode executions >> $PATHLOG1/logsh.txt 2>&1

echo "Fin ETL n8n executions" >> $PATHLOG1/logsh.txt
date >> $PATHLOG1/logsh.txt
