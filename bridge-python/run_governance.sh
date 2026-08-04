#!/bin/bash
#
#Script Name    : run_governance.sh
#
#******************************************************************************
#* Version   Fecha Inicio              Author                 Description     *
#* -------  --------------   --------------------------   ------------------  *
#*  1.0      04/08/2026              n8n-zabbix-bridge     Auditoria de gobernanza n8n -> Zabbix *
#******************************************************************************
export PATHLOG1=/opt/bmc/ETLs/n8n/monworkflows
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8
cd $PATHLOG1 || exit 1

echo "Inicio ETL n8n governance" >> $PATHLOG1/logsh.txt
date >> $PATHLOG1/logsh.txt

flock -n $PATHLOG1/.governance.lock \
  $PATHLOG1/python/bin/python3 $PATHLOG1/monitor.py --mode governance >> $PATHLOG1/logsh.txt 2>&1

echo "Fin ETL n8n governance" >> $PATHLOG1/logsh.txt
date >> $PATHLOG1/logsh.txt
