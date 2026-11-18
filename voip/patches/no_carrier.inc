				/* VoLTE: il modem non manda ^DSCI a fine chiamata, quindi NO CARRIER e'
				   l'unico avviso. Senza chiudere qui, la chiamata resta aperta nel modulo
				   e le successive non vengono piu' instradate. Linea singola, avviso di
				   chiamata spento: NO CARRIER chiude le chiamate in corso. */
				{
					struct cpvt * dead;
					AST_LIST_TRAVERSE_SAFE_BEGIN(&pvt->chans, dead, entry)
					{
						if (dead != &pvt->sys_chan && dead->state != CALL_STATE_RELEASED)
							change_channel_state(dead, CALL_STATE_RELEASED, 0);
					}
					AST_LIST_TRAVERSE_SAFE_END;
					pvt->ring = 0;
					pvt->dialing = 0;
					pvt->cwaiting = 0;
				}
